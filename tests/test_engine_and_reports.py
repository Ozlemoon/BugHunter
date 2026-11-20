"""Hunt orchestration, run history and report exporters."""

import json
import xml.etree.ElementTree as ET

import pytest
from starlette.testclient import TestClient

from core import reporting
from core.config import Settings
from core.engine import HuntConfig, HuntSession
from core.models import FixStatus, Severity, TargetType, TestCriterion
from core.storage import RunStore
from tests.helpers import API_BASE, AUTH, WEB_BASE, load_app


def session_for(path, tmp_path):
    """Each client loads the app fresh from disk, like an auto-reloading server after a patch."""
    settings = Settings(patch_roots=(tmp_path,), reports_dir=tmp_path / "reports")
    base = API_BASE if path.name == "sample_api.py" else WEB_BASE
    return HuntSession(
        settings=settings,
        client_factory=lambda: TestClient(load_app(path), raise_server_exceptions=False, base_url=base),
        settle_seconds=0,
    )


def api_config(path, **overrides):
    values = dict(target_url=f"{API_BASE}/openapi.json", target_type=TargetType.API,
                  auth_header=AUTH["Authorization"], source_file=str(path))
    values.update(overrides)
    return HuntConfig(**values)


def fix_for(run, test_id):
    return next(f for f in run.findings if f.evidence.test_id == test_id).proposal


def test_run_collects_findings_sorted_by_severity_and_persists(api_copy, tmp_path):
    session = session_for(api_copy, tmp_path)
    run = session.start(api_config(api_copy))

    assert run.summary.failed_tests == 6
    assert run.summary.fixable_findings == 5
    ranks = [f.evidence.severity.rank for f in run.findings]
    assert ranks == sorted(ranks, reverse=True)
    assert run.findings[0].evidence.severity == Severity.CRITICAL
    assert session.store.load(run.run_id).summary == run.summary
    assert session.store.list()[0].run_id == run.run_id


def test_approved_fix_is_verified_clean(api_copy, tmp_path):
    session = session_for(api_copy, tmp_path)
    run = session.start(api_config(api_copy))
    proposal = fix_for(run, "TC-API-BOUND-NEG-POST-orders")

    report = session.approve(run.run_id, proposal.fix_id)

    assert report.verdict == "CLEAN_FIX" and not report.rolled_back
    assert proposal.status == FixStatus.APPLIED
    patched = api_copy.read_text(encoding="utf-8")
    assert "\n    if order.quantity <= 0:\n" in patched  # the real guard, not the "# if ..." comment
    assert patched.count("Quantity must be greater than zero") == 2  # comment + new guard


def test_fix_that_breaks_unauthenticated_clients_is_rolled_back(api_copy, tmp_path):
    # Without credentials configured, requiring admin auth breaks the functional DELETE check.
    session = session_for(api_copy, tmp_path)
    run = session.start(api_config(api_copy, auth_header=None))
    proposal = fix_for(run, "TC-API-SEC-NOAUTH-DELETE-products-product_id")
    original = api_copy.read_text(encoding="utf-8")

    report = session.approve(run.run_id, proposal.fix_id)

    assert report.verdict == "REGRESSION_DETECTED"
    assert report.rolled_back and proposal.status == FixStatus.ROLLED_BACK
    assert api_copy.read_text(encoding="utf-8") == original


def test_source_file_outside_patch_roots_is_refused(api_copy, tmp_path):
    session = session_for(api_copy, tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="patch roots"):
        session.start(api_config(api_copy))


def test_run_without_source_file_reports_evidence_only(api_copy, tmp_path):
    run = session_for(api_copy, tmp_path).start(api_config(api_copy, source_file=None))
    assert run.findings and all(f.proposal is None for f in run.findings)


def test_config_validation():
    with pytest.raises(ValueError):
        HuntConfig(target_url="http://x", criteria=[])
    config = HuntConfig(target_url="  http://x  ", auth_header="  ", criteria=[TestCriterion.BOUNDARY] * 2)
    assert config.target_url == "http://x" and config.auth_header is None
    assert config.criteria == [TestCriterion.BOUNDARY]


def test_run_store_rejects_path_traversal(tmp_path):
    store = RunStore(tmp_path)
    assert store.load("../../etc/passwd") is None
    assert store.list() == []


def test_reports_render_in_every_format(web_copy, tmp_path):
    session = session_for(web_copy, tmp_path)
    run = session.start(HuntConfig(target_url=f"{WEB_BASE}/", target_type=TargetType.WEB, source_file=str(web_copy)))

    data = json.loads(reporting.render(run, "json"))
    assert data["run_id"] == run.run_id

    junit = ET.fromstring(reporting.render(run, "junit").split("\n", 1)[1])
    failures = sum(int(suite.get("failures")) for suite in junit.iter("testsuite"))
    assert failures == run.summary.failed_tests == len(junit.findall(".//failure"))

    html = reporting.render(run, "html")
    assert run.run_id in html
    assert "<script>bughunter_xss_probe()" not in html  # probe text is escaped in the evidence
    with pytest.raises(ValueError):
        reporting.render(run, "pdf")
