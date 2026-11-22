"""E-mailing a run's report: message content, share links, access control and SMTP errors."""

import smtplib

import pytest
from starlette.testclient import TestClient

import web.app as web_app
from core import mailer
from core.config import Settings, get_settings
from core.engine import HuntSession
from tests.helpers import API_BASE, load_app

PASSWORD = "dogru-sifre-123"


class FakeSMTP:
    """Stands in for smtplib.SMTP and remembers every message it was asked to send."""
    sent = []
    fail_with = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with
        self.calls.append(("login", user))

    def send_message(self, message):
        FakeSMTP.sent.append(message)


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.fail_with = [], None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    for key, value in {"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "bughunter@example.com",
                       "SMTP_USER": "bughunter@example.com", "SMTP_PASSWORD": "app-pass",
                       "PUBLIC_URL": "https://bughunter.example.com"}.items():
        monkeypatch.setenv(f"BUGHUNTER_{key}", value)
    get_settings.cache_clear()
    yield FakeSMTP
    get_settings.cache_clear()


@pytest.fixture
def session(api_copy, tmp_path, monkeypatch):
    current = HuntSession(
        settings=Settings(patch_roots=(tmp_path,), reports_dir=tmp_path / "reports"),
        client_factory=lambda: TestClient(load_app(api_copy), raise_server_exceptions=False, base_url=API_BASE),
        settle_seconds=0,
    )
    monkeypatch.setattr(web_app, "session", current)
    return current


def signed_in(username):
    client = TestClient(web_app.app)
    assert client.post("/api/auth/register", json={"username": username, "password": PASSWORD}).status_code == 200
    return client


def hunt(client):
    response = client.post("/api/runs", json={"target_url": f"{API_BASE}/openapi.json", "criteria": ["BOUNDARY"]})
    assert response.status_code == 200, response.text
    return response.json()


def html_part(message):
    return message.get_body(preferencelist=("html",)).get_content()


def test_report_email_shows_system_name_counts_and_view_button(session, smtp):
    client = signed_in("ayse")
    run = hunt(client)
    response = client.post(f"/api/runs/{run['run_id']}/email",
                           json={"recipients": "ali@example.com; ekip@example.com", "note": "Kritiklere bakalım."})
    assert response.status_code == 200, response.text
    assert response.json()["recipients"] == ["ali@example.com", "ekip@example.com"]

    message = smtp.sent[0]
    assert message["To"] == "ali@example.com, ekip@example.com"
    assert message["From"] == "bughunter@example.com"
    summary = run["summary"]
    name = mailer.system_name(run["config"]["target_url"])
    assert name in message["Subject"]
    assert f"{summary['total_tests'] - summary['failed_tests']}/{summary['total_tests']} başarılı" in message["Subject"]

    html = html_part(message)
    assert "Test edilen sistem" in html and f">{name}</h1>" in html
    for label, value in (("Toplam test", summary["total_tests"]), ("Başarılı", summary["passed_tests"]),
                         ("Başarısız", summary["failed_tests"])):
        assert label in html and f">{value}</div>" in html
    assert "Raporu görüntüle" in html and "Kritiklere bakalım." in html and "ayse" in html
    assert 'src="cid:' in html  # the logo travels inside the message

    text = message.get_body(preferencelist=("plain",)).get_content()
    assert f"Toplam test: {summary['total_tests']}" in text

    attachments = list(message.iter_attachments())
    assert [a.get_filename() for a in attachments] == [f"bughunter-{run['run_id']}.html"]
    assert run["run_id"] in attachments[0].get_content()


def test_view_button_opens_the_html_report_without_logging_in(session, smtp):
    client = signed_in("ayse")
    run = hunt(client)
    report_url = client.post(f"/api/runs/{run['run_id']}/email", json={"recipients": ["ali@example.com"]}).json()["report_url"]
    assert report_url.startswith("https://bughunter.example.com/r/")
    assert report_url in html_part(smtp.sent[0])

    path = report_url.removeprefix("https://bughunter.example.com")
    shared = TestClient(web_app.app).get(path)  # a recipient: no account, no cookie
    assert shared.status_code == 200 and "BugHunter test raporu" in shared.text and run["run_id"] in shared.text
    assert shared.headers["x-robots-tag"] == "noindex"

    assert TestClient(web_app.app).get("/r/uydurma-token").status_code == 404
    assert TestClient(web_app.app).get(f"/api/runs/{run['run_id']}/report.html").status_code == 401


def test_share_links_expire(session):
    run = session.start(web_app.HuntConfig(target_url=f"{API_BASE}/openapi.json", criteria=["BOUNDARY"]))
    token = session.store.create_share(run.run_id, days=-1)
    assert session.store.load_shared(token) is None
    assert session.store.load_shared(session.store.create_share(run.run_id)).run_id == run.run_id


def test_only_the_owner_can_email_a_run(session, smtp):
    ayse, mehmet = signed_in("ayse"), signed_in("mehmet")
    run = hunt(ayse)
    assert mehmet.post(f"/api/runs/{run['run_id']}/email", json={"recipients": "x@example.com"}).status_code == 404
    assert TestClient(web_app.app).post(f"/api/runs/{run['run_id']}/email", json={"recipients": "x@example.com"}).status_code == 401
    assert smtp.sent == []


@pytest.mark.parametrize("recipients, message", [
    ("", "En az bir"),
    ("ali@example", "Geçersiz"),
    ("ali@example.com\r\nBcc: kurban@example.com", "Geçersiz"),  # header injection
    (",".join(f"k{i}@example.com" for i in range(11)), "en fazla 10"),
])
def test_bad_recipients_are_refused(session, smtp, recipients, message):
    client = signed_in("ayse")
    run = hunt(client)
    response = client.post(f"/api/runs/{run['run_id']}/email", json={"recipients": recipients})
    assert response.status_code == 422 and message in response.json()["detail"]
    assert smtp.sent == []


def test_unconfigured_smtp_explains_how_to_set_it_up(session, monkeypatch):
    for key in ("SMTP_HOST", "SMTP_FROM"):
        monkeypatch.delenv(f"BUGHUNTER_{key}", raising=False)
    get_settings.cache_clear()
    try:
        client = signed_in("ayse")
        run = hunt(client)
        assert client.get("/api/mail/status").json()["configured"] is False
        response = client.post(f"/api/runs/{run['run_id']}/email", json={"recipients": "ali@example.com"})
        assert response.status_code == 503 and "BUGHUNTER_SMTP_HOST" in response.json()["detail"]
    finally:
        get_settings.cache_clear()


def test_smtp_login_failure_is_explained(session, smtp):
    smtp.fail_with = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    client = signed_in("ayse")
    run = hunt(client)
    response = client.post(f"/api/runs/{run['run_id']}/email", json={"recipients": "ali@example.com"})
    assert response.status_code == 502 and "uygulama şifresi" in response.json()["detail"]


def test_recipient_parsing():
    assert mailer.parse_recipients(["a@x.com, b@y.org", "A@x.com"]) == ["a@x.com", "b@y.org"]
    assert mailer.system_name("https://petstore3.swagger.io/api/v3/openapi.json") == "petstore3.swagger.io"


def test_mail_menu_item_and_view(session):
    html = signed_in("ayse").get("/").text
    assert 'data-view-link="mail"' in html and 'data-view="mail" hidden' in html
    for text in ("Rapor gönder", "Raporu e-postayla gönder", "Alıcılar", "Raporu görüntüle", "E-posta önizlemesi"):
        assert text in html
