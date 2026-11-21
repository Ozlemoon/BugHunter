"""Remediation advice: right advice per finding, honest about low risk and likely false alarms."""

from starlette.testclient import TestClient

from core.analyzer import BugAnalyzer
from core.config import Settings
from core.engine import HuntConfig, HuntSession
from core.models import (
    AssertionType,
    Severity,
    TargetType,
    TestAssertion,
    TestCase,
    TestCriterion,
    TestResult,
)
from core.remediation import advise
from tests.helpers import API_BASE, AUTH, WEB_BASE, load_app


def run_for(path, tmp_path, config):
    base = API_BASE if path.name == "sample_api.py" else WEB_BASE
    session = HuntSession(
        settings=Settings(patch_roots=(tmp_path,), reports_dir=tmp_path / "reports"),
        client_factory=lambda: TestClient(load_app(path), raise_server_exceptions=False, base_url=base),
        settle_seconds=0,
    )
    return session.start(config)


def by_test(run):
    return {f.evidence.test_id: f for f in run.findings}


def test_every_api_finding_gets_specific_advice(api_copy, tmp_path):
    run = run_for(api_copy, tmp_path, HuntConfig(target_url=f"{API_BASE}/openapi.json",
                                                   auth_header=AUTH["Authorization"]))
    findings = by_test(run)
    expected = {
        "TC-API-BOUND-NONEXIST-user_id-GET-users-user_id-profile": "Sunucu çöküyor (HTTP 500)",
        "TC-API-SEC-NOAUTH-DELETE-products-product_id": "Kimlik doğrulaması olmadan",
        "TC-API-SEC-BADTOKEN-DELETE-products-product_id": "Geçersiz token kabul ediliyor",
        "TC-API-BOUND-NEG-POST-orders": "Negatif sayılar kabul ediliyor",
        "TC-API-PERF-GET-products-heavy-search": "Yavaş yanıt",
    }
    for test_id, headline in expected.items():
        remediation = findings[test_id].remediation
        assert remediation.headline.startswith(headline), remediation.headline
        assert remediation.steps and remediation.priority
    assert findings["TC-API-BOUND-NONEXIST-user_id-GET-users-user_id-profile"].remediation.priority.startswith("Acil")


def test_findings_are_ordered_high_risk_first(api_copy, tmp_path):
    run = run_for(api_copy, tmp_path, HuntConfig(target_url=f"{API_BASE}/openapi.json",
                                                   auth_header=AUTH["Authorization"]))
    ranks = [f.evidence.severity.rank for f in run.findings]
    assert ranks == sorted(ranks, reverse=True)
    assert run.findings[0].evidence.severity == Severity.CRITICAL


def test_web_advice_names_the_missing_headers_and_marks_them_low_risk(web_copy, tmp_path):
    run = run_for(web_copy, tmp_path, HuntConfig(target_url=f"{WEB_BASE}/", target_type=TargetType.WEB))
    headers = by_test(run)["TC-WEB-SEC-HEADERS"].remediation
    assert "X-Content-Type-Options" in headers.headline and "Content-Security-Policy" in headers.headline
    assert headers.severity == Severity.LOW and headers.priority.startswith("Düşük") and headers.note
    assert by_test(run)["TC-WEB-FORM-XSS-1"].remediation.headline.startswith("Yansıyan XSS")
    assert run.findings[-1].evidence.severity == Severity.LOW


def _functional_404():
    test = TestCase(id="TC-API-FUNC-GET-pets-id", target_type=TargetType.API, criterion=TestCriterion.FUNCTIONAL,
                    title="Happy Path", description="d", method="GET", url="https://x/pets/1", endpoint="/pets/{id}",
                    assertions=[TestAssertion(assertion_type=AssertionType.STATUS_CODE_IN,
                                              expected_value=[200], description="2xx")])
    result = TestResult(test_id=test.id, passed=False, status_code=404,
                        failed_assertions=["Expected status in [200], but received 404"])
    return test, result


def test_made_up_ids_on_real_apis_are_flagged_as_probable_false_alarms():
    test, result = _functional_404()
    remediation = advise(test, result, BugAnalyzer().analyze_failure(test, result))
    assert remediation.noteworthy is False
    assert remediation.severity == Severity.LOW
    assert "yanlış alarm" in remediation.note


def test_slow_response_close_to_the_threshold_is_not_alarming():
    test = TestCase(id="TC-API-PERF-GET-x", target_type=TargetType.API, criterion=TestCriterion.PERFORMANCE,
                    title="SLA", description="d", url="https://x/", tags=["api", "performance"],
                    assertions=[TestAssertion(assertion_type=AssertionType.RESPONSE_TIME_UNDER_MS,
                                              expected_value=500.0, description="fast")])
    near = TestResult(test_id=test.id, passed=False, status_code=200, elapsed_ms=620,
                      failed_assertions=["Response time 620.0ms exceeded SLA threshold of 500.0ms"])
    far = near.model_copy(update={"elapsed_ms": 2400})
    near_advice = advise(test, near, BugAnalyzer().analyze_failure(test, near))
    far_advice = advise(test, far, BugAnalyzer().analyze_failure(test, far))
    assert near_advice.noteworthy is False and "ağ gecikmesi" in near_advice.note
    assert far_advice.noteworthy is True and far_advice.severity == Severity.MEDIUM


def test_findings_name_their_error_source_and_location(api_copy, tmp_path):
    from core.remediation import ErrorSource

    run = run_for(api_copy, tmp_path, HuntConfig(target_url=f"{API_BASE}/openapi.json",
                                                   auth_header=AUTH["Authorization"]))
    findings = by_test(run)
    crash = findings["TC-API-BOUND-NONEXIST-user_id-GET-users-user_id-profile"].remediation
    assert crash.source == ErrorSource.SERVER_CRASH
    assert crash.source_side.startswith("Sunucu tarafı")
    assert crash.location.startswith("GET /users/{user_id}/profile işleyicisi")

    negative = findings["TC-API-BOUND-NEG-POST-orders"].remediation
    assert negative.source == ErrorSource.INPUT_VALIDATION
    assert "negatif kabul edilen alanlar: quantity" in negative.location

    assert findings["TC-API-SEC-NOAUTH-DELETE-products-product_id"].remediation.source == ErrorSource.AUTH


def test_run_recommendations_group_findings_by_source(api_copy, tmp_path):
    run = run_for(api_copy, tmp_path, HuntConfig(target_url=f"{API_BASE}/openapi.json",
                                                   auth_header=AUTH["Authorization"]))
    recs = run.recommendations
    assert recs, "a failing run must produce run-level advice"
    assert sum(r.count for r in recs) == len(run.findings)
    assert len({r.source for r in recs}) == len(recs)
    noteworthy = [r for r in recs if r.noteworthy]
    assert recs[:len(noteworthy)] == noteworthy, "real problems come before 'probably nothing' groups"
    assert recs[0].severity == Severity.CRITICAL and recs[0].advice and recs[0].locations


def test_web_findings_point_to_page_output_and_headers(web_copy, tmp_path):
    from core.remediation import ErrorSource

    run = run_for(web_copy, tmp_path, HuntConfig(target_url=f"{WEB_BASE}/", target_type=TargetType.WEB))
    sources = {f.remediation.source: f.remediation for f in run.findings}
    assert "yansıyan parametre: q" in sources[ErrorSource.OUTPUT_ENCODING].location
    assert "yanıt başlıkları" in sources[ErrorSource.SERVER_CONFIG].location


def test_unreachable_request_is_blamed_on_the_network():
    from core.remediation import ErrorSource

    test = TestCase(id="t", target_type=TargetType.API, criterion=TestCriterion.FUNCTIONAL, title="t",
                    description="d", url="https://api.ornek.test/items")
    result = TestResult(test_id="t", passed=False, error_message="DNS çözümlenemedi")
    evidence = BugAnalyzer().analyze_failure(test, result)
    remediation = advise(test, result, evidence)
    assert remediation.source == ErrorSource.NETWORK and not remediation.noteworthy
    assert remediation.location.startswith("api.ornek.test")
