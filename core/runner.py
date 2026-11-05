"""
Test Runner Engine.
Executes generated test cases against live API or Web targets,
measures response times, verifies assertions, and collects test results.
"""

import time
from typing import List, Optional
import httpx

from core.models import (
    AssertionType,
    TestCase,
    TestResult,
)


class TestRunner:
    """Executes TestCase objects and validates assertions against responses."""
    __test__ = False

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def run_suite(self, tests: List[TestCase]) -> List[TestResult]:
        """Runs a list of test cases synchronously."""
        results: List[TestResult] = []
        with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
            for test in tests:
                res = self.run_test(client, test)
                results.append(res)
        return results

    def run_test(self, client: Optional[httpx.Client], test: TestCase) -> TestResult:
        """Executes a single test case and evaluates all assertions."""
        if client is None:
            with httpx.Client(timeout=self.timeout, follow_redirects=False) as c:
                return self._execute_test_request(c, test)
        return self._execute_test_request(client, test)

    def _execute_test_request(self, client: httpx.Client, test: TestCase) -> TestResult:
        failed_assertions: List[str] = []
        status_code: Optional[int] = None
        elapsed_ms: float = 0.0
        response_body: Optional[str] = None
        response_headers: dict = {}
        error_message: Optional[str] = None

        start_time = time.perf_counter()
        try:
            req_kwargs = {
                "method": test.method,
                "url": test.url,
                "headers": test.headers,
                "params": test.query_params or None,
            }

            if test.payload is not None:
                if test.is_form_data:
                    req_kwargs["data"] = test.payload
                else:
                    req_kwargs["json"] = test.payload

            response = client.request(**req_kwargs)
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            status_code = response.status_code
            response_body = response.text
            response_headers = dict(response.headers)

            # Evaluate assertions
            for assertion in test.assertions:
                passed, reason = self._evaluate_assertion(
                    assertion,
                    status_code=status_code,
                    elapsed_ms=elapsed_ms,
                    body=response_body,
                    headers=response_headers
                )
                if not passed:
                    failed_assertions.append(reason)

        except Exception as ex:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            error_message = f"Execution exception: {type(ex).__name__} - {str(ex)}"
            failed_assertions.append(error_message)

        passed = len(failed_assertions) == 0

        return TestResult(
            test_id=test.id,
            passed=passed,
            status_code=status_code,
            elapsed_ms=round(elapsed_ms, 2),
            response_body=response_body[:2000] if response_body else None,
            response_headers=response_headers,
            failed_assertions=failed_assertions,
            error_message=error_message
        )

    def _evaluate_assertion(
        self,
        assertion,
        status_code: Optional[int],
        elapsed_ms: float,
        body: Optional[str],
        headers: dict
    ) -> (bool, str):
        atype = assertion.assertion_type
        exp = assertion.expected_value

        if atype == AssertionType.STATUS_CODE_EQUALS:
            if status_code != exp:
                return False, f"Expected status {exp}, but received {status_code}"

        elif atype == AssertionType.STATUS_CODE_IN:
            if status_code not in exp:
                return False, f"Expected status in {exp}, but received {status_code}"

        elif atype == AssertionType.NO_SERVER_ERROR:
            if status_code is not None and 500 <= status_code <= 599:
                return False, f"Server returned 5xx Internal Server Error (HTTP {status_code})"

        elif atype == AssertionType.RESPONSE_TIME_UNDER_MS:
            if elapsed_ms > exp:
                return False, f"Response time {elapsed_ms:.1f}ms exceeded SLA threshold of {exp:.1f}ms"

        elif atype == AssertionType.BODY_CONTAINS:
            if not body or str(exp) not in body:
                return False, f"Response body does not contain expected substring '{exp}'"

        elif atype == AssertionType.HEADER_EXISTS:
            if exp.lower() not in [h.lower() for h in headers.keys()]:
                return False, f"Expected header '{exp}' was not found in response"

        return True, ""
