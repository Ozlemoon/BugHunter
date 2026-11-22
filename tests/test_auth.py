"""Login screen, accounts, sessions and per-user "Önceki testlerim" history."""

import json

import pytest
from starlette.testclient import TestClient

import web.app as web_app
from core import auth
from core.auth import AuthError, UserStore, hash_password, verify_password
from core.config import Settings, get_settings
from core.database import Database
from core.engine import HuntSession
from core.storage import RunStore
from tests.helpers import API_BASE, load_app

PASSWORD = "dogru-sifre-123"


@pytest.fixture
def session(api_copy, tmp_path, monkeypatch):
    settings = Settings(patch_roots=(tmp_path,), reports_dir=tmp_path / "reports")
    current = HuntSession(
        settings=settings,
        client_factory=lambda: TestClient(load_app(api_copy), raise_server_exceptions=False, base_url=API_BASE),
        settle_seconds=0,
    )
    monkeypatch.setattr(web_app, "session", current)
    return current


def new_client():
    return TestClient(web_app.app)


def register(client, username, password=PASSWORD):
    return client.post("/api/auth/register", json={"username": username, "password": password})


def hunt(client, api_copy):
    response = client.post("/api/runs", json={"target_url": f"{API_BASE}/openapi.json",
                                              "criteria": ["BOUNDARY"], "source_file": str(api_copy)})
    assert response.status_code == 200, response.text
    return response.json()["run_id"]


# -- passwords and accounts -------------------------------------------------

def test_passwords_are_salted_hashes_never_plain_text(tmp_path):
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second and PASSWORD not in first
    assert verify_password(PASSWORD, first) and not verify_password("yanlis-sifre", first)
    assert not verify_password(PASSWORD, "bozuk$kayit")

    users = UserStore(Database(tmp_path / "x.db"))
    users.create("ayse", PASSWORD)
    raw = (tmp_path / "x.db").read_bytes()
    assert PASSWORD.encode() not in raw


def test_account_rules(tmp_path):
    users = UserStore(Database(tmp_path / "x.db"))
    users.create("Ayse", PASSWORD)
    with pytest.raises(AuthError, match="zaten"):
        users.create("ayse", PASSWORD)  # usernames are case-insensitive
    with pytest.raises(AuthError, match="8 karakter"):
        users.create("mehmet", "kisa")
    with pytest.raises(AuthError, match="Kullanıcı adı"):
        users.create("a b", PASSWORD)
    assert users.authenticate("AYSE", PASSWORD).username == "Ayse"
    with pytest.raises(AuthError, match="hatalı"):
        users.authenticate("ayse", "yanlis-sifre")
    with pytest.raises(AuthError, match="hatalı"):
        users.authenticate("olmayan", PASSWORD)  # same message: no hint which users exist


def test_repeated_wrong_passwords_lock_the_account_briefly(tmp_path, monkeypatch):
    users = UserStore(Database(tmp_path / "x.db"))
    users.create("ayse", PASSWORD)
    for _ in range(auth.MAX_FAILED_LOGINS):
        with pytest.raises(AuthError):
            users.authenticate("ayse", "yanlis-sifre")
    with pytest.raises(AuthError, match="Çok fazla"):
        users.authenticate("ayse", PASSWORD)  # even the right password waits
    clock = auth.time.monotonic() + auth.LOCKOUT_SECONDS + 1
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock)
    assert users.authenticate("ayse", PASSWORD).username == "ayse"


def test_session_tokens_are_stored_only_as_hashes(tmp_path):
    users = UserStore(Database(tmp_path / "x.db"))
    user = users.create("ayse", PASSWORD)
    token = users.create_session(user)
    assert users.user_for_session(token) == user
    with Database(tmp_path / "x.db").connect() as db:
        stored = db.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert stored != token and token not in stored
    users.end_session(token)
    assert users.user_for_session(token) is None
    assert users.user_for_session("uydurma") is None


# -- web flow ------------------------------------------------------------------

def test_dashboard_requires_login(session):
    client = new_client()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    for path in ("/api/runs", "/api/config", "/api/runs/20000101-000000-abcdef"):
        blocked = client.get(path)
        assert blocked.status_code == 401 and blocked.headers["x-bughunter-login"] == "required"
    assert client.post("/api/runs", json={"target_url": "http://x"}).status_code == 401
    assert client.get("/healthz").status_code == 200


def test_login_page_offers_first_account_then_login(session):
    client = new_client()
    page = client.get("/login")
    assert page.status_code == 200 and 'data-first-user="true"' in page.text
    for text in ("Kullanıcı adı", "Şifre", "Giriş yap", "Hesap oluştur", "Önceki testlerim"):
        assert text in page.text
    assert register(client, "ayse").status_code == 200
    assert client.get("/login", follow_redirects=False).status_code == 303  # already signed in
    assert 'data-first-user="false"' in new_client().get("/login").text


def test_register_login_logout_cycle(session):
    client = new_client()
    response = register(client, "ayse")
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/api/auth/me").json() == {"username": "ayse"}
    assert "ayse" in client.get("/").text

    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401

    wrong = client.post("/api/auth/login", json={"username": "ayse", "password": "yanlis-sifre"})
    assert wrong.status_code == 401 and wrong.json()["detail"] == "Kullanıcı adı veya şifre hatalı."
    assert client.post("/api/auth/login", json={"username": "ayse", "password": PASSWORD}).status_code == 200
    assert client.get("/api/auth/me").json() == {"username": "ayse"}


def test_signup_can_be_closed_except_for_the_first_account(session, monkeypatch):
    monkeypatch.setenv("BUGHUNTER_ALLOW_SIGNUP", "false")
    get_settings.cache_clear()
    try:
        assert register(new_client(), "ilk").status_code == 200
        refused = register(new_client(), "ikinci")
        assert refused.status_code == 403 and "kapalı" in refused.json()["detail"]
        assert 'data-allow-signup="false"' in new_client().get("/login").text
    finally:
        get_settings.cache_clear()


def test_each_user_sees_only_their_own_previous_tests(session, api_copy):
    ayse, mehmet = new_client(), new_client()
    register(ayse, "ayse")
    register(mehmet, "mehmet")
    run_id = hunt(ayse, api_copy)

    mine = ayse.get("/api/runs").json()["runs"]
    assert [r["run_id"] for r in mine] == [run_id] and mine[0]["failed_tests"] > 0
    assert mehmet.get("/api/runs").json()["runs"] == []
    for path in (f"/api/runs/{run_id}", f"/api/runs/{run_id}/report.html"):
        assert ayse.get(path).status_code == 200
        assert mehmet.get(path).status_code == 404  # not even told that it exists
    fix_id = next(f["proposal"]["fix_id"] for f in ayse.get(f"/api/runs/{run_id}").json()["findings"] if f["proposal"])
    assert mehmet.post(f"/api/runs/{run_id}/fixes/{fix_id}/reject").status_code == 404


def test_previous_tests_survive_a_server_restart(session, api_copy, tmp_path):
    client = new_client()
    register(client, "ayse")
    run_id = hunt(client, api_copy)
    fresh = HuntSession(settings=session.settings)  # a new process reading the same database
    assert [r.run_id for r in fresh.store.list(owner_id=1)] == [run_id]
    assert fresh.get_run(run_id, owner_id=1).run_id == run_id
    assert fresh.get_run(run_id, owner_id=2) is None


def test_cross_site_posts_are_refused(session):
    client = new_client()
    evil = client.post("/api/auth/register", json={"username": "ayse", "password": PASSWORD},
                       headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    same = client.post("/api/auth/register", json={"username": "ayse", "password": PASSWORD},
                       headers={"Origin": "http://testserver"})
    assert same.status_code == 200


# -- earlier file-based history ---------------------------------------------

def test_old_json_runs_are_imported_and_given_to_the_first_account(session, api_copy, tmp_path):
    run = session.start(web_app.HuntConfig(target_url=f"{API_BASE}/openapi.json", criteria=["BOUNDARY"]),
                        persist=False)
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / f"{run.run_id}.json").write_text(run.model_dump_json(), encoding="utf-8")
    (legacy / "20260101-000000-abcdef.json").write_text(json.dumps({"broken": True}), encoding="utf-8")

    store = RunStore(tmp_path / "imported.db", legacy_dir=legacy)
    assert [r.run_id for r in store.list()] == [run.run_id]
    RunStore(store.db, legacy_dir=legacy)  # importing twice adds nothing
    assert len(store.list()) == 1

    users = UserStore(store.db)
    ayse = users.create("ayse", PASSWORD)
    assert store.assign_unowned(ayse.id) == 1
    assert [r.run_id for r in store.list(owner_id=ayse.id)] == [run.run_id]
