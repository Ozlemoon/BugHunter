"""Command line: headless scan exit codes, report export and argument wiring."""

import xml.etree.ElementTree as ET

import pytest
from starlette.testclient import TestClient

import cli
import main
from core.config import Settings
from core.engine import HuntSession
from tests.helpers import API_BASE, load_app


@pytest.fixture
def in_process_session(api_copy, tmp_path, monkeypatch):
    settings = Settings(patch_roots=(tmp_path,), reports_dir=tmp_path / "reports")

    def factory():
        return HuntSession(
            settings=settings,
            client_factory=lambda: TestClient(load_app(api_copy), raise_server_exceptions=False, base_url=API_BASE),
            settle_seconds=0,
        )

    monkeypatch.setattr(cli, "HuntSession", factory)
    return settings


SCAN = ["scan", "--url", f"{API_BASE}/openapi.json", "--auth", "Bearer admin-token", "--quiet"]


def test_scan_fails_build_on_high_findings_and_writes_junit(in_process_session, tmp_path):
    junit = tmp_path / "out" / "junit.xml"
    assert main.main([*SCAN, "--junit", str(junit)]) == cli.EXIT_FINDINGS
    root = ET.parse(junit).getroot()
    assert root.tag == "testsuites" and int(root.get("failures")) == 6


def test_scan_threshold_controls_exit_code(in_process_session):
    assert main.main([*SCAN, "--fail-on", "NEVER"]) == cli.EXIT_OK
    assert main.main([*SCAN, "--criteria", "PERFORMANCE", "--fail-on", "HIGH"]) == cli.EXIT_OK  # only MEDIUM
    assert main.main([*SCAN, "--criteria", "PERFORMANCE", "--fail-on", "MEDIUM"]) == cli.EXIT_FINDINGS


def test_scan_reports_unreachable_target_as_error(tmp_path, monkeypatch):
    monkeypatch.setenv("BUGHUNTER_REPORTS_DIR", str(tmp_path))
    assert main.main(["scan", "--url", "http://127.0.0.1:9/openapi.json", "--quiet"]) == cli.EXIT_ERROR


def test_runs_and_report_export(in_process_session, tmp_path, capsys):
    main.main([*SCAN, "--fail-on", "NEVER"])
    run_id = cli.HuntSession().store.list()[0].run_id
    assert main.main(["runs"]) == cli.EXIT_OK
    output = tmp_path / "report.html"
    assert main.main(["report", run_id, "--format", "html", "-o", str(output)]) == cli.EXIT_OK
    assert run_id in output.read_text(encoding="utf-8")
    assert main.main(["report", "20000101-000000-abcdef"]) == cli.EXIT_ERROR


def test_unknown_criterion_is_rejected():
    with pytest.raises(SystemExit):
        main.main(["scan", "--criteria", "NOPE"])


def test_help_and_aliases_parse():
    parser = main.build_parser()
    assert parser.parse_args(["cli", "--url", "x"]).handler is cli.cmd_hunt
    assert main.main([]) == 0
