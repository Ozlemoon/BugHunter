"""Web dashboard API: hunts, fix approval flow, history, reports and access control."""

import pytest
from starlette.testclient import TestClient

import web.app as web_app
from core.config import Settings, get_settings
from core.engine import HuntSession
from tests.helpers import API_BASE, load_app

QUANTITY_GUARD = "\n    if order.quantity <= 0:\n"  # real code line, not the "# if ..." comment


@pytest.fixture
def client(api_copy, tmp_path, monkeypatch):
    settings = Settings(patch_roots=(tmp_path,), reports_dir=tmp_path / "reports")
    session = HuntSession(
        settings=settings,
        client_factory=lambda: TestClient(load_app(api_copy), raise_server_exceptions=False, base_url=API_BASE),
        settle_seconds=0,
    )
    monkeypatch.setattr(web_app, "session", session)
    return TestClient(web_app.app)


def start(client, api_copy, **overrides):
    body = {"target_url": f"{API_BASE}/openapi.json", "target_type": "API",
            "auth_header": "Bearer admin-token", "source_file": str(api_copy)}
    body.update(overrides)
    response = client.post("/api/runs", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_dashboard_homepage_renders():
    response = TestClient(web_app.app).get("/")
    assert response.status_code == 200
    for text in ("BugHunter AI", "Test çalışma alanı", "Test rehberi", "Proje belgelerine sor", "Çalışma geçmişi"):
        assert text in response.text
    assert response.headers["x-frame-options"] == "DENY"


def test_healthz_and_config():
    client = TestClient(web_app.app)
    assert client.get("/healthz").json()["status"] == "ok"
    config = client.get("/api/config").json()
    assert {d["target_type"] for d in config["demo_targets"]} == {"API", "WEB"}


def test_run_returns_findings_and_never_echoes_credentials(client, api_copy):
    data = start(client, api_copy)
    assert data["summary"]["failed_tests"] == 6
    assert data["summary"]["fixable_findings"] == 5
    assert "auth_header" not in data["config"]
    assert all(f["proposal"]["status"] == "PENDING" for f in data["findings"])


def test_approve_then_rollback_flow(client, api_copy):
    data = start(client, api_copy)
    fix = next(f["proposal"] for f in data["findings"] if f["evidence"]["test_id"] == "TC-API-BOUND-NEG-POST-orders")
    base = f"/api/runs/{data['run_id']}/fixes/{fix['fix_id']}"
    original = api_copy.read_text(encoding="utf-8")

    approved = client.post(f"{base}/approve").json()
    assert approved["report"]["verdict"] == "CLEAN_FIX"
    assert approved["proposal"]["status"] == "APPLIED"
    assert QUANTITY_GUARD in api_copy.read_text(encoding="utf-8")

    assert client.post(f"{base}/approve").status_code == 409  # already applied
    rolled = client.post(f"{base}/rollback")
    assert rolled.status_code == 200 and rolled.json()["proposal"]["status"] == "ROLLED_BACK"
    assert api_copy.read_text(encoding="utf-8") == original


def test_reject_and_unknown_fix(client, api_copy):
    data = start(client, api_copy)
    fix_id = data["findings"][0]["proposal"]["fix_id"]
    assert client.post(f"/api/runs/{data['run_id']}/fixes/{fix_id}/reject").json()["proposal"]["status"] == "REJECTED"
    assert client.post(f"/api/runs/{data['run_id']}/fixes/FIX-NOPE/approve").status_code == 404
    assert client.post(f"/api/runs/{data['run_id']}/fixes/{fix_id}/rollback").status_code == 409


def test_history_and_report_downloads(client, api_copy):
    data = start(client, api_copy)
    runs = client.get("/api/runs").json()["runs"]
    assert runs[0]["run_id"] == data["run_id"] and runs[0]["worst_severity"] == "CRITICAL"
    assert client.get(f"/api/runs/{data['run_id']}").json()["run_id"] == data["run_id"]

    for ext, media in (("html", "text/html"), ("xml", "application/xml"), ("json", "application/json")):
        response = client.get(f"/api/runs/{data['run_id']}/report.{ext}")
        assert response.status_code == 200 and response.headers["content-type"].startswith(media)
    assert client.get(f"/api/runs/{data['run_id']}/report.pdf").status_code == 404
    assert client.get("/api/runs/20000101-000000-abcdef").status_code == 404


def test_source_files_outside_patch_roots_are_refused(client, api_copy):
    body = {"target_url": f"{API_BASE}/openapi.json", "source_file": "C:/Windows/win.ini"}
    response = client.post("/api/runs", json=body)
    assert response.status_code == 422 and "patch roots" in response.json()["detail"]


def test_legacy_arbitrary_path_rollback_endpoint_is_gone(client):
    assert client.post("/api/rollback-fix", json={"file_path": "core/config.py"}).status_code in (404, 405)


def test_legacy_run_hunt_alias_still_works(client):
    response = client.post("/api/run-hunt", json={"target_url": f"{API_BASE}/openapi.json",
                                                  "criteria": ["BOUNDARY", "UNKNOWN"]})
    assert response.status_code == 200 and response.json()["summary"]["total_tests"] > 0


def test_api_token_protects_api_routes(client, monkeypatch):
    monkeypatch.setenv("BUGHUNTER_API_TOKEN", "s3cret")
    get_settings.cache_clear()
    try:
        assert client.get("/api/runs").status_code == 401
        assert client.get("/api/runs", headers={"X-BugHunter-Token": "wrong"}).status_code == 401
        assert client.get("/api/runs", headers={"X-BugHunter-Token": "s3cret"}).status_code == 200
        assert client.get("/healthz").status_code == 200  # health checks stay open
    finally:
        get_settings.cache_clear()


def test_unreachable_target_returns_502(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "session", HuntSession(settings=Settings(reports_dir=tmp_path)))
    response = TestClient(web_app.app).post("/api/runs", json={"target_url": "http://127.0.0.1:9/openapi.json"})
    assert response.status_code == 502


def test_page_declares_its_version_so_a_stale_server_is_detected():
    html = TestClient(web_app.app).get("/").text
    assert f'data-version="{web_app.__version__}"' in html
    assert 'id="server-warning"' in html


def test_findings_carry_remediation_and_page_groups_by_risk(client, api_copy):
    data = start(client, api_copy)
    first = data["findings"][0]
    assert first["evidence"]["severity"] == "CRITICAL"
    assert first["remediation"]["headline"] and first["remediation"]["steps"]
    html = TestClient(web_app.app).get("/").text
    for text in ("Yüksek riskli bulgular", "Düşük riskli ve bilgi amaçlı", "Çözüm önerisi", "text-rose-700"):
        assert text in html
