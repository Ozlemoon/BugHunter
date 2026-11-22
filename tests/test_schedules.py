"""Scheduled digests: timing, the compiled e-mail, pausing, failures and access control."""

import dataclasses
import smtplib
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

import web.app as web_app
from core import mailer, schedules
from core.config import get_settings
from core.engine import HuntSession
from core.schedules import Scheduler, ScheduleStore, first_run
from tests.helpers import API_BASE, load_app
from tests.test_mail import FakeSMTP

PASSWORD = "dogru-sifre-123"


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.fail_with = [], None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    for key, value in {"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "bughunter@example.com",
                       "PUBLIC_URL": "https://bughunter.example.com"}.items():
        monkeypatch.setenv(f"BUGHUNTER_{key}", value)
    get_settings.cache_clear()
    yield FakeSMTP
    get_settings.cache_clear()


@pytest.fixture
def session(api_copy, tmp_path, monkeypatch, smtp):
    current = HuntSession(
        settings=dataclasses.replace(get_settings(), patch_roots=(tmp_path,), reports_dir=tmp_path / "reports"),
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
    response = client.post("/api/runs", json={"target_url": f"{API_BASE}/openapi.json"})
    assert response.status_code == 200, response.text
    return response.json()


def schedule(client, **overrides):
    body = {"recipients": "ben@example.com", "interval_days": 3, "send_time": "09:00"}
    body.update(overrides)
    response = client.post("/api/schedules", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def make_due(session, schedule_id, when):
    with session.store.db.connect() as db:
        db.execute("UPDATE email_schedules SET next_run_at = ? WHERE id = ?", (when.isoformat(), schedule_id))


def tick(session, now):
    return Scheduler(lambda: session).tick(now)


# -- timing ----------------------------------------------------------------------

def test_first_run_is_the_next_time_of_day_on_the_local_clock():
    local = datetime(2026, 11, 22, 8, 30).astimezone()
    assert first_run("09:00", local).astimezone() == local.replace(hour=9, minute=0)
    assert first_run("08:00", local).astimezone() == (local + timedelta(days=1)).replace(hour=8, minute=0)


def test_creating_a_schedule_sets_the_first_send_time(session):
    created = schedule(signed_in("ayse"), send_time="23:59", interval_days=7)
    assert created["active"] and created["interval_days"] == 7 and created["recipients"] == ["ben@example.com"]
    next_run = datetime.fromisoformat(created["next_run_at"])
    assert timedelta(0) < next_run - datetime.now(timezone.utc) <= timedelta(days=1)
    assert next_run.astimezone().strftime("%H:%M") == "23:59"


# -- the digest -----------------------------------------------------------------

def test_due_schedule_sends_one_digest_of_the_periods_bugs(session, smtp):
    client = signed_in("ayse")
    run = hunt(client)
    created = schedule(client)
    now = datetime.now(timezone.utc) + timedelta(minutes=1)
    make_due(session, created["id"], now - timedelta(seconds=30))

    assert tick(session, now) == 1
    message = smtp.sent[0]
    name = mailer.system_name(run["config"]["target_url"])
    assert message["To"] == "ben@example.com"
    assert "son 3 gün" in message["Subject"] and f"{run['summary']['total_tests']} test" in message["Subject"]
    html = message.get_body(preferencelist=("html",)).get_content()
    assert name in html and "Raporu görüntüle" in html and "https://bughunter.example.com/r/" in html
    assert f">{run['summary']['failed_tests']}</div>" in html  # Başarısız
    assert "CRITICAL" in html
    first_bug = next(f["remediation"]["headline"] for f in run["findings"] if f["remediation"]["noteworthy"])
    assert first_bug in html

    stored = ScheduleStore(session.store.db).get(created["id"])
    assert stored.last_sent_at == now and stored.last_error is None
    assert stored.next_run_at > now and stored.next_run_at.astimezone().strftime("%H:%M") == "09:00"
    assert tick(session, now + timedelta(minutes=5)) == 0  # not again until the next period
    assert len(smtp.sent) == 1


def test_missed_periods_are_sent_once_not_one_by_one(session, smtp):
    client = signed_in("ayse")
    created = schedule(client, interval_days=1)
    now = datetime.now(timezone.utc)
    make_due(session, created["id"], now - timedelta(days=5))  # dashboard was off for five days
    assert tick(session, now) == 1
    assert tick(session, now + timedelta(seconds=1)) == 0
    assert ScheduleStore(session.store.db).get(created["id"]).next_run_at > now


def test_empty_period_says_no_new_tests(session, smtp):
    created = schedule(signed_in("ayse"))
    now = datetime.now(timezone.utc)
    make_due(session, created["id"], now)
    tick(session, now)
    assert "yeni test yok" in smtp.sent[0]["Subject"]
    assert "yeni test çalıştırılmadı" in smtp.sent[0].get_body(preferencelist=("html",)).get_content()


def test_site_scope_and_old_runs_are_left_out(session, smtp):
    client = signed_in("ayse")
    hunt(client)
    created = schedule(client, site="baska-site.example.com")
    now = datetime.now(timezone.utc)
    make_due(session, created["id"], now)
    tick(session, now)
    assert "baska-site.example.com" in smtp.sent[0]["Subject"] and "yeni test yok" in smtp.sent[0]["Subject"]

    everything = schedule(client, interval_days=1)
    later = now + timedelta(days=2)  # the run is now older than the 1-day period
    make_due(session, everything["id"], later)
    tick(session, later)
    assert "yeni test yok" in smtp.sent[-1]["Subject"]


def test_paused_schedules_are_skipped_and_resume_in_the_future(session, smtp):
    client = signed_in("ayse")
    created = schedule(client)
    assert client.patch(f"/api/schedules/{created['id']}", json={"active": False}).json()["active"] is False
    now = datetime.now(timezone.utc)
    make_due(session, created["id"], now - timedelta(days=1))
    assert tick(session, now) == 0

    resumed = client.patch(f"/api/schedules/{created['id']}", json={"active": True}).json()
    assert resumed["active"] and datetime.fromisoformat(resumed["next_run_at"]) > now  # no burst on resume
    assert smtp.sent == []


def test_failed_send_is_recorded_and_retried_later(session, smtp):
    smtp.fail_with = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    session.settings = dataclasses.replace(session.settings, smtp_user="bughunter@example.com")  # forces login
    created = schedule(signed_in("ayse"))
    now = datetime.now(timezone.utc)
    make_due(session, created["id"], now)
    assert tick(session, now) == 0
    stored = ScheduleStore(session.store.db).get(created["id"])
    assert "uygulama şifresi" in stored.last_error.lower()
    assert stored.next_run_at == now + schedules.RETRY_AFTER


def test_send_now_endpoint(session, smtp):
    client = signed_in("ayse")
    hunt(client)
    created = schedule(client)
    response = client.post(f"/api/schedules/{created['id']}/send-now")
    assert response.status_code == 200 and response.json()["runs"] == 1
    assert response.json()["schedule"]["last_sent_at"]
    assert response.json()["schedule"]["next_run_at"] == created["next_run_at"]  # regular timing unchanged
    assert len(smtp.sent) == 1


# -- validation & access ----------------------------------------------------------

@pytest.mark.parametrize("overrides, status", [
    ({"recipients": "olmaz"}, 422),
    ({"interval_days": 0}, 422),
    ({"interval_days": 31}, 422),
    ({"send_time": "25:00"}, 422),
    ({"send_time": "9"}, 422),
])
def test_invalid_schedules_are_refused(session, overrides, status):
    body = {"recipients": "ben@example.com", "interval_days": 3, "send_time": "09:00", **overrides}
    assert signed_in("ayse").post("/api/schedules", json=body).status_code == status


def test_users_only_see_and_change_their_own_schedules(session, smtp):
    ayse, mehmet = signed_in("ayse"), signed_in("mehmet")
    created = schedule(ayse)
    assert [s["id"] for s in ayse.get("/api/schedules").json()["schedules"]] == [created["id"]]
    assert mehmet.get("/api/schedules").json()["schedules"] == []
    base = f"/api/schedules/{created['id']}"
    assert mehmet.patch(base, json={"active": False}).status_code == 404
    assert mehmet.post(f"{base}/send-now").status_code == 404
    assert mehmet.delete(base).status_code == 404
    assert TestClient(web_app.app).get("/api/schedules").status_code == 401
    assert ayse.delete(base).json()["status"] == "deleted"
    assert ayse.get("/api/schedules").json()["schedules"] == []


def test_schedule_limit_per_user(session):
    client = signed_in("ayse")
    for _ in range(schedules.MAX_SCHEDULES_PER_USER):
        schedule(client)
    response = client.post("/api/schedules", json={"recipients": "ben@example.com", "interval_days": 3, "send_time": "09:00"})
    assert response.status_code == 422 and "En fazla" in response.json()["detail"]


def test_page_has_the_schedule_card(session):
    html = signed_in("ayse").get("/").text
    for text in ("Zamanlanmış rapor", 'id="schedule-form"', "3 günde bir", "Zamanla", 'id="schedule-list"'):
        assert text in html


def test_digest_counts_each_bug_once_across_runs(session, smtp):
    client = signed_in("ayse")
    first, second = hunt(client), hunt(client)  # the same bugs, found twice
    created = schedule(client)
    now = datetime.now(timezone.utc)
    make_due(session, created["id"], now)
    tick(session, now)
    runs = session.store.list_since(1, now - timedelta(days=3))
    context = mailer.digest_context(runs, now - timedelta(days=3), now, 3, None, {})
    site = context["sites"][0]
    distinct = {f["evidence"]["test_id"] for f in first["findings"] if f["remediation"]["noteworthy"]}
    assert site["runs"] == 2 and site["bug_count"] == len(distinct)
    assert sum(site["severity"].values()) == len(distinct)
    assert site["total"] == first["summary"]["total_tests"] + second["summary"]["total_tests"]
