"""
Unit and integration tests for Stage 6: RegressionVerifier.
Tests defect resolution verification, regression detection, and before/after metrics.
"""

from starlette.testclient import TestClient
from core.models import (
    AssertionType,
    FixProposal,
    TargetType,
    TestAssertion,
    TestCase,
    TestCriterion,
    TestResult,
)
from core.runner import TestRunner
from core.verifier import RegressionVerifier
from playground.sample_api import app as api_app


def test_regression_verifier_clean_fix():
    # Setup happy test and boundary test
    happy_test = TestCase(
        id="TC-API-HAPPY-PRODUCTS",
        target_type=TargetType.API,
        criterion=TestCriterion.FUNCTIONAL,
        title="Get Products",
        description="Verify products endpoint returns 200",
        method="GET",
        url="/products",
        assertions=[
            TestAssertion(
                assertion_type=AssertionType.STATUS_CODE_EQUALS,
                expected_value=200,
                description="Status must be 200"
            )
        ]
    )

    boundary_test = TestCase(
        id="TC-API-BOUND-NEG-ORDER",
        target_type=TargetType.API,
        criterion=TestCriterion.BOUNDARY,
        title="Negative order quantity",
        description="Verify negative order quantity is rejected",
        method="POST",
        url="/orders",
        payload={"product_id": 1, "quantity": 1, "customer_id": 101},  # Simulated valid after fix
        assertions=[
            TestAssertion(
                assertion_type=AssertionType.STATUS_CODE_IN,
                expected_value=[200, 201],
                description="Status must be 201"
            )
        ]
    )

    all_tests = [happy_test, boundary_test]

    # Simulated before results: happy_test passed, boundary_test failed
    before_results = [
        TestResult(test_id="TC-API-HAPPY-PRODUCTS", passed=True, status_code=200),
        TestResult(test_id="TC-API-BOUND-NEG-ORDER", passed=False, status_code=422, failed_assertions=["Failed"])
    ]

    proposal = FixProposal(
        fix_id="FIX-BUG-API-BOUND-NEG-ORDER",
        bug_id="BUG-API-BOUND-NEG-ORDER",
        target_file="playground/sample_api.py",
        title="Fix negative order quantity",
        explanation="Added quantity > 0 validation",
        unified_diff="--- a\n+++ b",
        approved=True
    )

    client = TestClient(api_app, raise_server_exceptions=False)
    runner = TestRunner()
    verifier = RegressionVerifier()

    report = verifier.verify(
        runner=runner,
        client=client,
        all_tests=all_tests,
        target_test=boundary_test,
        before_results=before_results,
        proposal=proposal
    )

    assert report.bug_fixed is True
    assert report.verdict == "CLEAN_FIX"
    assert len(report.regressions_detected) == 0
    assert report.before_pass_count == 1
    assert report.after_pass_count == 2


def test_regression_verifier_detects_regression():
    # If a previously passing test now fails
    test_1 = TestCase(
        id="TC-TEST-1",
        target_type=TargetType.API,
        criterion=TestCriterion.FUNCTIONAL,
        title="Endpoint 1",
        description="Endpoint 1",
        method="GET",
        url="/non-existent-broken-route",  # Will fail (404)
        assertions=[
            TestAssertion(
                assertion_type=AssertionType.STATUS_CODE_EQUALS,
                expected_value=200,
                description="Expected 200"
            )
        ]
    )

    # Simulated state: test_1 was marked as passing before, but now fails
    before_results = [
        TestResult(test_id="TC-TEST-1", passed=True, status_code=200)
    ]

    proposal = FixProposal(
        fix_id="FIX-1",
        bug_id="BUG-1",
        target_file="test.py",
        title="Fix 1",
        explanation="Explanation",
        unified_diff="",
        approved=True
    )

    client = TestClient(api_app, raise_server_exceptions=False)
    runner = TestRunner()
    verifier = RegressionVerifier()

    # Target test passes, but test_1 regressed
    report = verifier.verify(
        runner=runner,
        client=client,
        all_tests=[test_1],
        target_test=TestCase(
            id="TC-TARGET",
            target_type=TargetType.API,
            criterion=TestCriterion.FUNCTIONAL,
            title="Target",
            description="Target",
            method="GET",
            url="/products",
            assertions=[TestAssertion(assertion_type=AssertionType.STATUS_CODE_EQUALS, expected_value=200, description="200")]
        ),
        before_results=before_results,
        proposal=proposal
    )

    assert report.bug_fixed is True
    assert report.verdict == "REGRESSION_DETECTED"
    assert len(report.regressions_detected) == 1
    assert "TC-TEST-1" in report.regressions_detected[0]
