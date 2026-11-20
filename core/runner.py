"""
Test Runner Engine.
Executes generated test cases against live API or Web targets,
measures response times, verifies assertions, and collects test results.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import List, Optional, Tuple

import httpx

from core import schema
from core.models import AssertionType, TestAssertion, TestCase, TestResult

logger = logging.getLogger(__name__)

MAX_STORED_BODY = 2000
USER_AGENT = "BugHunter-AI/1.1 (+https://github.com/Ozlemoon/BugHunter)"


class TestRunner:
    """Executes TestCase objects and validates assertions against responses."""
    __test__ = False

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, follow_redirects=False, headers={"User-Agent": USER_AGENT})

    def run_suite(
        self,
        tests: List[TestCase],
        client: Optional[httpx.Client] = None,
        on_result: Optional[Callable[[TestCase, TestResult], None]] = None,
    ) -> List[TestResult]:
        """Runs test cases sequentially (targets are usually stateful, so order matters)."""
        results: List[TestResult] = []
        if client is None:
            with self._client() as own_client:
                return self.run_suite(tests, own_client, on_result)
        for test in tests:
            result = self.run_test(client, test)
            results.append(result)
            if on_result:
                on_result(test, result)
        return results

    def run_test(self, client: Optional[httpx.Client], test: TestCase) -> TestResult:
        """Executes a single test case and evaluates all assertions."""
        if client is None:
            with self._client() as own_client:
                return self._execute_test_request(own_client, test)
        return self._execute_test_request(client, test)

    def _execute_test_request(self, client: httpx.Client, test: TestCase) -> TestResult:
        failed_assertions: List[str] = []
        status_code: Optional[int] = None
        response_body: Optional[str] = None
        response_headers: dict = {}
        error_message: Optional[str] = None

        req_kwargs = {
            "method": test.method,
            "url": test.url,
            "headers": test.headers,
            "params": test.query_params or None,
        }
        if test.payload is not None:
            req_kwargs["data" if test.is_form_data else "json"] = test.payload

        start_time = time.perf_counter()
        try:
            response = client.request(**req_kwargs)
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            status_code = response.status_code
            response_body = response.text
            response_headers = dict(response.headers)

            for assertion in test.assertions:
                passed, reason = self._evaluate_assertion(
                    assertion,
                    status_code=status_code,
                    elapsed_ms=elapsed_ms,
                    body=response_body,
                    headers=response_headers,
                )
                if not passed:
                    failed_assertions.append(reason)
        except Exception as ex:  # network errors, invalid URLs, in-process app crashes
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            error_message = f"Execution exception: {type(ex).__name__} - {ex}"
            failed_assertions.append(error_message)
            logger.warning("%s %s failed: %s", test.method, test.url, error_message)

        return TestResult(
            test_id=test.id,
            passed=not failed_assertions,
            status_code=status_code,
            elapsed_ms=round(elapsed_ms, 2),
            response_body=response_body[:MAX_STORED_BODY] if response_body else None,
            response_headers=response_headers,
            failed_assertions=failed_assertions,
            error_message=error_message,
        )

    def _evaluate_assertion(
        self,
        assertion: TestAssertion,
        status_code: Optional[int],
        elapsed_ms: float,
        body: Optional[str],
        headers: dict,
    ) -> Tuple[bool, str]:
        atype = assertion.assertion_type
        exp = assertion.expected_value
        lower_headers = {k.lower(): v for k, v in headers.items()}

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
            if str(exp).lower() not in lower_headers:
                return False, f"Expected header '{exp}' was not found in response"

        elif atype == AssertionType.NOT_REFLECTED_IN_HTML:
            is_html = "text/html" in lower_headers.get("content-type", "")
            if is_html and body and str(exp) in body:
                return False, "Input was reflected unescaped into an HTML response (reflected XSS)"

        elif atype == AssertionType.BODY_MATCHES_SCHEMA:
            if status_code is None or not 200 <= status_code < 300 or status_code == 204:
                return True, ""  # only meaningful for successful responses with a body
            try:
                document = json.loads(body or "")
            except ValueError:
                return False, "Response body is not valid JSON but a JSON schema is documented"
            violations = schema.validate(document, exp)
            if violations:
                return False, "Response does not match documented schema: " + "; ".join(violations)

        return True, ""
