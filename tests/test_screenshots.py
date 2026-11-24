"""Screenshots of critical web findings: selection, storage, access, reports and a real browser."""

import socket
import threading
import time

import pytest
import uvicorn
from starlette.testclient import TestClient

import web.app as web_app
from core import reporting, screenshots
from core.config import Settings
from core.engine import HuntConfig, HuntSession
from core.models import TargetType, TestCase, TestCriterion, TestResult
from tests.helpers import API_BASE, WEB_BASE, load_app

PNG = b"\x89PNG\r\n\x1a\nfake-image"
PASSWORD = "dogru-sifre-123"


def fake_capturer(calls=None, fail=False):
    def capture(findings, tests, results, headers):
        if calls is not None:
            calls.append((findings, tests, headers))
        if fail:
            raise RuntimeError("browser crashed")
        chosen = [tests[f.evidence.test_id] for f in findings
                  if screenshots.worth_capturing(f, tests.get(f.evidence.test_id or ""))]
        return {t.id: {"png": PNG, "caption": screenshots.caption_for(t, 500)} for t in chosen}
    return capture


def web_session(web_copy, tmp_path, **kwargs):
    return HuntSession(
        settings=Settings(patch_roots=(tmp_path,), reports_dir=tmp_path / "reports"),
        client_factory=lambda: TestClient(load_app(web_copy), raise_server_exceptions=False, base_url=WEB_BASE),
        settle_seconds=0, **kwargs)


def web_run(session, owner_id=None):
    return session.start(HuntConfig(target_url=f"{WEB_BASE}/", target_type=TargetType.WEB), owner_id=owner_id)


def received(body, content_type, status=500):
    """A TestResult as the runner leaves it: the whole body is kept in memory, not saved."""
    result = TestResult(test_id="T", passed=False, status_code=status, response_headers={"content-type": content_type},
                        response_body=(body or "")[:2000] or None)
    result._full_body = body
    return result


def make_case(method="GET", payload=None, is_form_data=False):
    return TestCase(id="T", target_type=TargetType.WEB, criterion=TestCriterion.BOUNDARY, title="t", description="d",
                    method=method, url=f"{WEB_BASE}/x", payload=payload, is_form_data=is_form_data)


# -- which findings ---------------------------------------------------------------------

def test_only_serious_replayable_findings_are_captured(web_copy, tmp_path):
    run = web_session(web_copy, tmp_path).start(HuntConfig(target_url=f"{WEB_BASE}/", target_type=TargetType.WEB),
                                                persist=False)
    finding = run.findings[0]
    assert finding.evidence.severity.value == "CRITICAL"
    page = received("<html><body>Internal Server Error</body></html>", "text/html; charset=utf-8")
    assert screenshots.worth_capturing(finding, make_case("POST", {"email": "x"}, is_form_data=True), page)
    assert screenshots.worth_capturing(finding, make_case("GET"), received("Internal Server Error", "text/plain"))
    assert not screenshots.worth_capturing(finding, make_case("GET"), received('{"a": 1}', "application/json"))
    assert not screenshots.worth_capturing(finding, make_case("GET"), received(None, "text/html"))  # nothing received
    assert not screenshots.worth_capturing(finding, None)
    low = next(f for f in run.findings if f.evidence.severity.value == "LOW")
    assert not screenshots.worth_capturing(low, make_case("GET"))


def test_capture_respects_the_limit_and_survives_one_bad_page(web_copy, tmp_path):
    run = web_session(web_copy, tmp_path).start(HuntConfig(target_url=f"{WEB_BASE}/", target_type=TargetType.WEB),
                                                persist=False)
    tests = {t.id: t for t in run.tests}
    serious = [f for f in run.findings if screenshots.worth_capturing(
        f, tests.get(f.evidence.test_id), next(r for r in run.results if r.test_id == f.evidence.test_id))]
    assert len(serious) >= 2

    class FakeBrowser:
        opened = closed = 0

        def __init__(self, *args):
            FakeBrowser.opened += 1

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            FakeBrowser.closed += 1

        def capture(self, test, result):
            if test.id == serious[0].evidence.test_id:
                raise screenshots.ScreenshotError("page hung")
            return PNG

    results = {r.test_id: r for r in run.results}
    shots = screenshots.capture_findings(run.findings, tests, results, "edge.exe", browser_factory=FakeBrowser)
    assert serious[0].evidence.test_id not in shots and serious[1].evidence.test_id in shots
    assert FakeBrowser.opened == FakeBrowser.closed == 1  # one browser for the batch, always closed
    limited = screenshots.capture_findings(run.findings, tests, results, "edge.exe", limit=1, browser_factory=FakeBrowser)
    assert len(limited) <= 1


# -- engine, storage, access, reports ----------------------------------------------------

def test_web_run_stores_screenshots_beside_the_findings(web_copy, tmp_path):
    calls = []
    session = web_session(web_copy, tmp_path, screenshotter=fake_capturer(calls))
    run = web_run(session)
    shot_findings = [f for f in run.findings if f.screenshot]
    assert shot_findings and all(f.evidence.severity.value in ("CRITICAL", "HIGH") for f in shot_findings)
    first = shot_findings[0]
    assert session.store.screenshot(run.run_id, first.evidence.test_id) == PNG
    assert session.store.load(run.run_id).findings[0].screenshot  # caption saved with the run
    assert "PNG" not in session.store.load(run.run_id).model_dump_json()  # images stay out of the run JSON

    html = reporting.render(run, "html", session.store.screenshots_for(run.run_id))
    assert html.count("data:image/png;base64,") == len(shot_findings) and "Ekran görüntüsü" in html
    assert "data:image/png" not in reporting.render(run, "html")


def test_api_runs_and_disabled_setting_take_no_screenshots(api_copy, web_copy, tmp_path):
    calls = []
    api = HuntSession(settings=Settings(patch_roots=(tmp_path,), reports_dir=tmp_path / "r1"),
                      client_factory=lambda: TestClient(load_app(api_copy), raise_server_exceptions=False, base_url=API_BASE),
                      settle_seconds=0, screenshotter=fake_capturer(calls))
    api.start(HuntConfig(target_url=f"{API_BASE}/openapi.json"))
    assert calls == []

    disabled = HuntSession(settings=Settings(reports_dir=tmp_path / "r2", screenshots_enabled=False),
                           client_factory=lambda: TestClient(load_app(web_copy), raise_server_exceptions=False,
                                                             base_url=WEB_BASE), settle_seconds=0)
    assert not any(f.screenshot for f in web_run(disabled).findings)


def test_a_crashing_browser_never_fails_the_run(web_copy, tmp_path):
    run = web_run(web_session(web_copy, tmp_path, screenshotter=fake_capturer(fail=True)))
    assert run.summary.failed_tests > 0 and not any(f.screenshot for f in run.findings)


def test_screenshot_endpoint_is_for_the_owner_only(web_copy, tmp_path, monkeypatch):
    session = web_session(web_copy, tmp_path, screenshotter=fake_capturer())
    monkeypatch.setattr(web_app, "session", session)
    ayse, mehmet = TestClient(web_app.app), TestClient(web_app.app)
    for client, name in ((ayse, "ayse"), (mehmet, "mehmet")):
        assert client.post("/api/auth/register", json={"username": name, "password": PASSWORD}).status_code == 200
    data = ayse.post("/api/runs", json={"target_url": f"{WEB_BASE}/", "target_type": "WEB"}).json()
    finding = next(f for f in data["findings"] if f["screenshot"])
    url = f"/api/runs/{data['run_id']}/screenshots/{finding['evidence']['test_id']}.png"

    image = ayse.get(url)
    assert image.status_code == 200 and image.headers["content-type"] == "image/png" and image.content == PNG
    assert mehmet.get(url).status_code == 404
    assert TestClient(web_app.app).get(url).status_code == 401
    assert ayse.get(f"/api/runs/{data['run_id']}/screenshots/YOK.png").status_code == 404
    assert "data:image/png;base64," in ayse.get(f"/api/runs/{data['run_id']}/report.html").text
    assert 'data-section="screenshot"' in ayse.get("/").text


# -- a real browser -------------------------------------------------------------------

def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.skipif(screenshots.find_browser() is None, reason="no Edge/Chrome/Chromium installed")
def test_real_browser_shows_the_received_page_cropped_to_the_problem(web_copy):
    import struct

    import httpx

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(load_app(web_copy), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        base = f"http://127.0.0.1:{port}"
        probe = '"><script>bughunter_xss_probe()</script>'
        crash = TestCase(id="crash", target_type=TargetType.WEB, criterion=TestCriterion.BOUNDARY, title="t",
                         description="d", method="POST", url=f"{base}/submit-contact", is_form_data=True,
                         payload={"name": "a", "email": "no-at-sign", "message": "m"})
        xss = TestCase(id="xss", target_type=TargetType.WEB, criterion=TestCriterion.SECURITY, title="t",
                       description="d", url=f"{base}/search", query_params={"q": probe})
        crash_response = httpx.post(crash.url, data=crash.payload)
        xss_response = httpx.get(xss.url, params=xss.query_params)
        server.should_exit = True  # the browser must not need the site any more: nothing is re-sent
        thread.join(timeout=10)

        with screenshots.open_browser(screenshots.find_browser()) as browser:
            crash_png = browser.capture(crash, received(crash_response.text, crash_response.headers["content-type"]))
            xss_png = browser.capture(xss, received(xss_response.text, xss_response.headers["content-type"], 200))
            shown = browser._call("Runtime.evaluate", returnByValue=True,
                                  expression="document.documentElement.innerText")["result"]["value"]
        png_signature = bytes([0x89]) + b"PNG"
        def size(png):
            return struct.unpack(">II", png[16:24])

        assert crash_png.startswith(png_signature) and xss_png.startswith(png_signature)
        assert size(crash_png)[1] < 200 and size(xss_png)[1] < 450  # cropped, not a blank 1280x800 window
        assert "XSS: sayfaya enjekte edilen kod" in shown  # the injected script really ran
    finally:
        server.should_exit = True
        thread.join(timeout=10)
