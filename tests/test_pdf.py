"""PDF reports: rendering, download, e-mail attachment (with HTML fallback) and the CLI."""

import smtplib

import pytest
from starlette.testclient import TestClient

import cli
import main
import web.app as web_app
from core import mailer, reporting, screenshots
from core.config import Settings, get_settings
from core.engine import HuntConfig, HuntSession
from core.models import TargetType
from tests.helpers import API_BASE, WEB_BASE, load_app
from tests.test_mail import FakeSMTP

FAKE_PDF = b"%PDF-1.7\nfake report\n%%EOF"
PASSWORD = "dogru-sifre-123"


def api_session(api_copy, tmp_path):
    return HuntSession(settings=Settings(patch_roots=(tmp_path,), reports_dir=tmp_path / "reports"),
                       client_factory=lambda: TestClient(load_app(api_copy), raise_server_exceptions=False,
                                                         base_url=API_BASE), settle_seconds=0)


def fake_pdf(calls=None):
    def render(run, shots=None, browser_path="", comparison=None):
        if calls is not None:
            calls.append((run.run_id, shots))
        return FAKE_PDF
    return render


def no_browser(run, shots=None, browser_path="", comparison=None):
    raise reporting.PdfError("PDF için Edge, Chrome veya Chromium bulunamadı; HTML rapor kullanılabilir.")


# -- rendering -----------------------------------------------------------------------

def test_missing_browser_is_a_clear_error(api_copy, tmp_path):
    run = api_session(api_copy, tmp_path).start(HuntConfig(target_url=f"{API_BASE}/openapi.json"), persist=False)
    with pytest.raises(reporting.PdfError, match="bulunamadı"):
        reporting.render_pdf(run, browser_path=str(tmp_path / "yok.exe"))
    with pytest.raises(ValueError):
        reporting.render(run, "pdf")  # binary: only through render_pdf


@pytest.mark.skipif(screenshots.find_browser() is None, reason="no Edge/Chrome/Chromium installed")
def test_real_pdf_of_a_web_run(web_copy, tmp_path):
    session = HuntSession(settings=Settings(reports_dir=tmp_path / "reports"),
                          client_factory=lambda: TestClient(load_app(web_copy), raise_server_exceptions=False,
                                                            base_url=WEB_BASE), settle_seconds=0)
    run = session.start(HuntConfig(target_url=f"{WEB_BASE}/", target_type=TargetType.WEB))
    pdf = reporting.render_pdf(run)
    assert pdf.startswith(b"%PDF") and pdf.rstrip().endswith(b"%%EOF") and b"/Page" in pdf
    assert len(pdf) > 10_000


# -- download & e-mail -------------------------------------------------------------------

@pytest.fixture
def client(api_copy, tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "session", api_session(api_copy, tmp_path))
    client = TestClient(web_app.app)
    assert client.post("/api/auth/register", json={"username": "ayse", "password": PASSWORD}).status_code == 200
    return client


def start(client):
    return client.post("/api/runs", json={"target_url": f"{API_BASE}/openapi.json", "criteria": ["BOUNDARY"]}).json()


def test_pdf_download(client, monkeypatch):
    monkeypatch.setattr(reporting, "render_pdf", fake_pdf())
    run_id = start(client)["run_id"]
    response = client.get(f"/api/runs/{run_id}/report.pdf")
    assert response.status_code == 200 and response.headers["content-type"] == "application/pdf"
    assert response.content == FAKE_PDF and f'bughunter-{run_id}.pdf' in response.headers["content-disposition"]

    monkeypatch.setattr(reporting, "render_pdf", no_browser)
    failed = client.get(f"/api/runs/{run_id}/report.pdf")
    assert failed.status_code == 503 and "HTML rapor" in failed.json()["detail"]


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.fail_with = [], None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("BUGHUNTER_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("BUGHUNTER_SMTP_FROM", "bughunter@example.com")
    get_settings.cache_clear()
    yield FakeSMTP
    get_settings.cache_clear()


def test_emailed_report_is_a_pdf(client, smtp, monkeypatch):
    calls = []
    monkeypatch.setattr(reporting, "render_pdf", fake_pdf(calls))
    run_id = start(client)["run_id"]
    response = client.post(f"/api/runs/{run_id}/email", json={"recipients": "ali@example.com"})
    assert response.status_code == 200 and response.json()["attachment"] == "pdf"
    assert calls and calls[0][0] == run_id

    message = smtp.sent[0]
    [attachment] = list(message.iter_attachments())
    assert attachment.get_content_type() == "application/pdf"
    assert attachment.get_filename() == f"bughunter-{run_id}.pdf" and attachment.get_content() == FAKE_PDF
    html = message.get_body(preferencelist=("html",)).get_content()
    assert f"bughunter-{run_id}.pdf" in html
    assert f"bughunter-{run_id}.pdf" in message.get_body(preferencelist=("plain",)).get_content()


def test_email_still_goes_with_html_when_pdf_fails(client, smtp, monkeypatch):
    monkeypatch.setattr(reporting, "render_pdf", no_browser)
    run_id = start(client)["run_id"]
    response = client.post(f"/api/runs/{run_id}/email", json={"recipients": "ali@example.com"})
    assert response.status_code == 200 and response.json()["attachment"] == "html"
    [attachment] = list(smtp.sent[0].iter_attachments())
    assert attachment.get_filename() == f"bughunter-{run_id}.html" and run_id in attachment.get_content()


def test_build_message_attaches_pdf_when_given(api_copy, tmp_path):
    run = api_session(api_copy, tmp_path).start(HuntConfig(target_url=f"{API_BASE}/openapi.json"), persist=False)
    message = mailer.build_message(run, ["a@example.com"], "b@example.com", "https://x/r/t", "ayse", pdf=FAKE_PDF)
    assert [a.get_content_type() for a in message.iter_attachments()] == ["application/pdf"]


def test_panel_offers_pdf(client):
    assert "['PDF', 'pdf']" in client.get("/").text


# -- command line ------------------------------------------------------------------------

def test_scan_writes_pdf(api_copy, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "HuntSession", lambda: api_session(api_copy, tmp_path))
    monkeypatch.setattr(reporting, "render_pdf", fake_pdf())
    target = tmp_path / "out" / "rapor.pdf"
    main.main(["scan", "--url", f"{API_BASE}/openapi.json", "--quiet", "--fail-on", "NEVER", "--pdf", str(target)])
    assert target.read_bytes() == FAKE_PDF

    monkeypatch.setattr(reporting, "render_pdf", no_browser)
    missing = tmp_path / "out" / "yok.pdf"
    assert main.main(["scan", "--url", f"{API_BASE}/openapi.json", "--quiet", "--fail-on", "NEVER",
                      "--pdf", str(missing)]) == cli.EXIT_OK  # no browser: other reports still written
    assert not missing.exists()
