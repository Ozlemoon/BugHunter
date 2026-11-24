"""Comparing a run with the previous test of the same system: new, fixed, persisting bugs."""

import pytest
from starlette.testclient import TestClient

import web.app as web_app
from core import comparison, mailer, reporting
from core.config import Settings
from core.engine import HuntSession
from core.models import Severity
from tests.helpers import API_BASE, load_app

PASSWORD = "dogru-sifre-123"


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


def hunt(client, criteria=("BOUNDARY", "SECURITY")):
    response = client.post("/api/runs", json={"target_url": f"{API_BASE}/openapi.json", "criteria": list(criteria)})
    assert response.status_code == 200, response.text
    return response.json()


def without(run, test_id, passed):
    """A copy of `run` in which the bug found by `test_id` is gone (its test passing or not run at all)."""
    copy = run.model_copy(deep=True)
    copy.findings = [f for f in copy.findings if f.evidence.test_id != test_id]
    if passed:
        for result in copy.results:
            if result.test_id == test_id:
                result.passed = True
    else:
        copy.results = [r for r in copy.results if r.test_id != test_id]
        copy.tests = [t for t in copy.tests if t.id != test_id]
    return copy


# -- core ---------------------------------------------------------------------------

def test_new_fixed_persisting_and_not_retested(session):
    run = session.start(session_config())
    ids = [f.evidence.test_id for f in run.findings if not f.remediation or f.remediation.noteworthy]
    assert len(ids) >= 3, "the sample API should have several real bugs"
    new_id, fixed_id, skipped_id = ids[:3]

    previous = without(run, new_id, passed=True)          # not there before → new now
    current = without(without(run, fixed_id, passed=True),  # passes now → fixed
                      skipped_id, passed=False)            # not run now → not re-tested, not "fixed"
    diff = comparison.compare(current, previous)

    assert [i["test_id"] for i in diff["new"]] == [new_id]
    assert [i["test_id"] for i in diff["fixed"]] == [fixed_id]
    assert [i["test_id"] for i in diff["not_retested"]] == [skipped_id]
    assert diff["counts"]["persisting"] == len(ids) - 3
    assert diff["verdict"] == "mixed" and diff["previous"]["run_id"] == previous.run_id
    assert comparison.headline(diff).startswith("Önceki teste göre: 1 yeni, 1 düzelen")


def test_verdicts_and_severity_change(session):
    run = session.start(session_config())
    first = run.findings[0].evidence.test_id
    assert comparison.compare(run, None)["verdict"] == "first"
    assert comparison.compare(run, run)["verdict"] == "same"
    assert comparison.compare(without(run, first, passed=True), run)["verdict"] == "better"
    assert comparison.compare(run, without(run, first, passed=True))["verdict"] == "worse"

    raised = run.model_copy(deep=True)
    raised.findings[0].evidence.severity = Severity.LOW if run.findings[0].evidence.severity != Severity.LOW else Severity.HIGH
    item = next(i for i in comparison.compare(raised, run)["persisting"] if i["test_id"] == first)
    assert item["previous_severity"] == run.findings[0].evidence.severity.value


def session_config():
    from core.engine import HuntConfig
    from core.models import TargetType, TestCriterion
    return HuntConfig(target_url=f"{API_BASE}/openapi.json", target_type=TargetType.API,
                      criteria=[TestCriterion.BOUNDARY, TestCriterion.SECURITY])


# -- store and web --------------------------------------------------------------------

def test_previous_run_is_the_same_target_of_the_same_user(session):
    ayse, mehmet = signed_in("ayse"), signed_in("mehmet")
    first = hunt(ayse)
    assert ayse.get(f"/api/runs/{first['run_id']}/compare").json()["verdict"] == "first"

    hunt(mehmet)  # another user's test of the same API is not "my previous test"
    second = hunt(ayse)
    diff = ayse.get(f"/api/runs/{second['run_id']}/compare").json()
    assert diff["previous"]["run_id"] == first["run_id"]
    assert diff["verdict"] == "same" and diff["counts"]["persisting"] > 0

    # Compare with a chosen run; the order is always older → newer.
    chosen = ayse.get(f"/api/runs/{first['run_id']}/compare", params={"base": second["run_id"]}).json()
    assert chosen["previous"]["run_id"] == first["run_id"] and chosen["current"]["run_id"] == second["run_id"]
    assert ayse.get(f"/api/runs/{first['run_id']}/compare", params={"base": first["run_id"]}).status_code == 422

    mehmet_run = mehmet.get("/api/runs").json()["runs"][0]["run_id"]
    assert ayse.get(f"/api/runs/{second['run_id']}/compare", params={"base": mehmet_run}).status_code == 404
    assert mehmet.get(f"/api/runs/{second['run_id']}/compare").status_code == 404


def test_fewer_criteria_are_not_reported_as_fixed(session):
    ayse = signed_in("ayse")
    hunt(ayse, ("BOUNDARY", "SECURITY"))
    second = hunt(ayse, ("BOUNDARY",))
    diff = ayse.get(f"/api/runs/{second['run_id']}/compare").json()
    assert diff["counts"]["fixed"] == 0
    assert diff["counts"]["not_retested"] > 0
    assert all(i["criterion"] == "SECURITY" for i in diff["not_retested"])


def test_report_and_email_show_the_change(session):
    ayse = signed_in("ayse")
    first = hunt(ayse)
    html = ayse.get(f"/api/runs/{first['run_id']}/report.html").text
    assert "Önceki teste göre değişim" not in html  # nothing to compare with yet

    second = hunt(ayse)
    html = ayse.get(f"/api/runs/{second['run_id']}/report.html").text
    assert "Önceki teste göre değişim" in html and first["run_id"] in html and "Devam eden" in html

    run = session.get_run(second["run_id"])
    previous = session.get_run(first["run_id"])
    diff = comparison.compare(run, previous)
    message = mailer.build_message(run, ["ali@example.com"], "bh@example.com", "https://x/r/t", sent_by="ayse",
                                   comparison=diff)
    text = message.get_body(preferencelist=("plain",)).get_content()
    assert f"Önceki teste göre: 0 yeni, 0 düzelen, {diff['counts']['persisting']} devam eden hata" in text
    assert "Önceki teste göre" in message.get_body(preferencelist=("html",)).get_content()
    no_change = mailer.build_message(run, ["ali@example.com"], "bh@example.com", "https://x/r/t", sent_by="ayse")
    assert "Önceki teste göre" not in no_change.get_body(preferencelist=("plain",)).get_content()
    assert "Önceki teste göre değişim" in reporting.render(run, "html", comparison=diff)


def test_dashboard_has_the_comparison_panel(session):
    html = signed_in("ayse").get("/").text
    for text in ('id="compare-panel"', "Önceki teste göre değişim", "İki testi karşılaştır", "/compare"):
        assert text in html
