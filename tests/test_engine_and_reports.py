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


def test_credentials_never_reach_disk_reports_or_repro_scripts(api_copy, tmp_path):
    secret = "Bearer s3cr3t-only-in-memory"  # must not appear anywhere in the target's source
    session = session_for(api_copy, tmp_path)
    run = session.start(api_config(api_copy, auth_header=secret))

    stored = next((tmp_path / "reports" / "runs").glob("*.json")).read_text(encoding="utf-8")
    assert secret not in stored
    for fmt in ("json", "junit", "html"):
        assert secret not in reporting.render(run, fmt)
    assert all(secret not in f.evidence.reproduction_code for f in run.findings)

    # The live run keeps the credential in memory so verification can still authenticate.
    assert run.config.auth_header == secret
    report = session.approve(run.run_id, fix_for(run, "TC-API-BOUND-NEG-POST-products").fix_id)
    assert report.verdict == "CLEAN_FIX"


def test_repro_script_reads_credentials_from_environment(api_copy, tmp_path):
    run = session_for(api_copy, tmp_path).start(api_config(api_copy))
    finding = next(f for f in run.findings if f.evidence.test_id == "TC-API-BOUND-NEG-POST-orders")
    script = finding.evidence.reproduction_code
    assert "os.environ['BUGHUNTER_AUTH_HEADER']" in script
    compile(script, "reproduce_bug.py", "exec")


@pytest.mark.parametrize("typed, expected", [
    ("quotes.toscrape.com", "https://quotes.toscrape.com"),
    ("  example.com/docs/openapi.json ", "https://example.com/docs/openapi.json"),
    ("http://127.0.0.1:8001/openapi.json", "http://127.0.0.1:8001/openapi.json"),
])
def test_target_urls_are_normalised(typed, expected):
    assert HuntConfig(target_url=typed).target_url == expected


@pytest.mark.parametrize("bad", ["ftp://example.com", "javascript:alert(1)", "   ", "https://"])
def test_invalid_target_urls_are_rejected(bad):
    with pytest.raises(ValueError):
        HuntConfig(target_url=bad)


def test_local_spec_files_are_not_turned_into_urls(api_copy):
    assert HuntConfig(target_url=str(api_copy)).target_url == str(api_copy)


def test_connection_errors_are_explained():
    import httpx

    from core.engine import describe_connection_error

    request = httpx.Request("GET", "https://nope.invalid/")
    dns = httpx.ConnectError("[Errno 11001] getaddrinfo failed", request=request)
    timeout = httpx.ReadTimeout("timed out", request=request)
    assert "Alan adı bulunamadı" in describe_connection_error(dns)
    assert "zaman aşımı" in describe_connection_error(timeout)
    assert "https://nope.invalid/" in describe_connection_error(dns)


def test_host_and_port_without_scheme_is_accepted():
    assert HuntConfig(target_url="localhost:8002/").target_url == "https://localhost:8002/"


def test_html_report_groups_findings_by_risk_with_advice(web_copy, tmp_path):
    run = session_for(web_copy, tmp_path).start(
        HuntConfig(target_url=f"{WEB_BASE}/", target_type=TargetType.WEB))
    html = reporting.render(run, "html")
    high, low = html.index("Yüksek riskli bulgular"), html.index("Düşük riskli ve bilgi amaçlı")
    assert high < low
    assert "Çözüm önerisi" in html and "Yansıyan XSS açığı" in html
    assert html.index("Yansıyan XSS açığı") < low  # high-risk advice sits above the low-risk group
