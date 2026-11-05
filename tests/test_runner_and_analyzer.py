"""
Integration and Unit Tests for Stage 4: TestRunner & BugAnalyzer.
Tests test execution and evidence collection against sample playground apps.
"""

from starlette.testclient import TestClient
from core.models import (
    AssertionType,
    TargetType,
    TestAssertion,
    TestCase,
    TestCriterion,
)
from core.runner import TestRunner
from core.analyzer import BugAnalyzer
from playground.sample_api import app as api_app
from playground.sample_web import app as web_app


def test_runner_detects_api_boundary_bug():
    # Negative quantity order should be rejected, but sample_api accepts it (201)
    boundary_test = TestCase(
        id="TC-API-BOUND-NEG-ORDER",
        target_type=TargetType.API,
        criterion=TestCriterion.BOUNDARY,
        title="Negative quantity order",
        description="Verify negative quantity order is rejected",
        method="POST",
        url="/orders",
        payload={"product_id": 1, "quantity": -5, "customer_id": 101},
        assertions=[
            TestAssertion(
                assertion_type=AssertionType.STATUS_CODE_IN,
                expected_value=[400, 422],
                description="Status must be 400 or 422"
            ),
            TestAssertion(
                assertion_type=AssertionType.NO_SERVER_ERROR,
                expected_value=True,
                description="No 5xx server error"
            )
        ]
    )

    client = TestClient(api_app, raise_server_exceptions=False)
    runner = TestRunner()
    result = runner.run_test(client, boundary_test)

    # In sample_api, it returns 201 Created instead of 400/422! So it should fail assertion!
    assert result.passed is False
    assert result.status_code == 201
    assert any("Expected status in [400, 422]" in f for f in result.failed_assertions)

    # BugAnalyzer should collect evidence
    analyzer = BugAnalyzer()
    evidence = analyzer.analyze_failure(boundary_test, result)

    assert evidence.bug_id == "BUG-API-BOUND-NEG-ORDER"
    assert evidence.severity == "HIGH"
    assert "Missing boundary constraint validation" in evidence.root_cause_hypothesis
    assert "reproduce_bug" in evidence.reproduction_code


def test_runner_detects_api_500_server_crash():
    # User profile for user 102 crashes with 500 KeyError
    crash_test = TestCase(
        id="TC-API-CRASH-PROFILE",
        target_type=TargetType.API,
        criterion=TestCriterion.BOUNDARY,
        title="User 102 profile lookup",
        description="Lookup profile for user 102",
        method="GET",
        url="/users/102/profile",
        assertions=[
            TestAssertion(
                assertion_type=AssertionType.NO_SERVER_ERROR,
                expected_value=True,
                description="Must not return 500"
            )
        ]
    )

    client = TestClient(api_app, raise_server_exceptions=False)
    runner = TestRunner()
    result = runner.run_test(client, crash_test)

    assert result.passed is False
    assert result.status_code == 500

    analyzer = BugAnalyzer()
    evidence = analyzer.analyze_failure(crash_test, result)

    assert evidence.severity == "CRITICAL"
    assert "Unhandled server exception" in evidence.root_cause_hypothesis


def test_runner_detects_web_form_crash():
    # Submitting contact form without '@' in email crashes with 500 in sample_web
    form_crash_test = TestCase(
        id="TC-WEB-FORM-CRASH",
        target_type=TargetType.WEB,
        criterion=TestCriterion.BOUNDARY,
        title="Web Form Invalid Email Crash",
        description="Submit form with invalid email",
        method="POST",
        url="/submit-contact",
        is_form_data=True,
        payload={"name": "Tester", "email": "invalid_email_no_at", "message": "hello"},
        assertions=[
            TestAssertion(
                assertion_type=AssertionType.NO_SERVER_ERROR,
                expected_value=True,
                description="Form submission must not crash with 500"
            )
        ]
    )

    client = TestClient(web_app, raise_server_exceptions=False)
    runner = TestRunner()
    result = runner.run_test(client, form_crash_test)

    assert result.passed is False
    assert result.status_code == 500

    analyzer = BugAnalyzer()
    evidence = analyzer.analyze_failure(form_crash_test, result)
    assert evidence.severity == "CRITICAL"
    assert "Unhandled server exception" in evidence.root_cause_hypothesis
