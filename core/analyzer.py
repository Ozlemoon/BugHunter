"""
Bug Analysis & Evidence Collection Engine.
Isolates failed tests, conducts root cause analysis, and synthesizes minimal,
independently reproducible Python evidence scripts.
"""

from __future__ import annotations

import json

from core.models import AssertionType, BugEvidence, Severity, TestCase, TestCriterion, TestResult
from core.redaction import AUTH_ENV_VAR, is_sensitive


class BugAnalyzer:
    """Analyzes test failures and generates structured evidence and reproduction scripts."""

    def analyze_failure(self, test: TestCase, result: TestResult) -> BugEvidence:
        """Constructs BugEvidence from a failed test and execution result."""
        return BugEvidence(
            bug_id=f"BUG-{test.id.removeprefix('TC-')}",
            title=f"Defect: {test.title}",
            target_type=test.target_type,
            criterion=test.criterion,
            target_url=test.url,
            method=test.method,
            test_id=test.id,
            endpoint=test.endpoint,
            status_code=result.status_code,
            tags=list(test.tags),
            payload=test.payload,
            expected_behavior=self._derive_expected_behavior(test),
            actual_behavior=self._derive_actual_behavior(result),
            reproduction_code=self._generate_reproduction_script(test),
            root_cause_hypothesis=self._hypothesize_root_cause(test, result),
            severity=self._determine_severity(test, result),
        )

    def _determine_severity(self, test: TestCase, result: TestResult) -> Severity:
        if result.status_code and 500 <= result.status_code <= 599:
            return Severity.CRITICAL
        if result.error_message:
            return Severity.HIGH  # target unreachable or request could not be sent
        if test.severity:
            return test.severity
        if test.criterion == TestCriterion.SECURITY:
            return Severity.CRITICAL
        if test.criterion == TestCriterion.BOUNDARY:
            return Severity.HIGH
        if test.criterion == TestCriterion.WEB_VALIDATION and result.status_code != 404:
            return Severity.HIGH
        return Severity.MEDIUM

    def _hypothesize_root_cause(self, test: TestCase, result: TestResult) -> str:
        reasons = "; ".join(result.failed_assertions)
        tags = set(test.tags)

        if result.error_message:
            return (
                f"The request could not be completed ({result.error_message}). The target may be down, "
                f"too slow for the configured timeout, or the URL may be wrong."
            )
        if result.status_code and 500 <= result.status_code <= 599:
            return (
                f"Unhandled server exception during {test.method} {test.url}. "
                f"The backend likely lacks input sanitization or safe dictionary/list indexing, "
                f"causing a raw 500 Internal Server Error: {reasons}"
            )
        if "xss" in tags:
            return (
                "Reflected XSS: user-controlled input is inserted into the HTML response without "
                "escaping, so an attacker-crafted link can run script in a victim's browser."
            )
        if "security-headers" in tags:
            return f"Missing HTTP hardening headers. The page is easier to frame or MIME-sniff: {reasons}"
        if "missing-required" in tags:
            return f"Required-field validation is missing; an empty body was accepted: {reasons}"
        if "wrong-type" in tags:
            return f"Type validation is missing; text in numeric fields was accepted: {reasons}"
        if test.criterion == TestCriterion.BOUNDARY:
            return (
                f"Missing boundary constraint validation. The service accepted invalid/negative input "
                f"without returning HTTP 400 or 422 Bad Request: {reasons}"
            )
        if test.criterion == TestCriterion.SECURITY:
            return (
                f"Missing or unenforced access control. Endpoint {test.method} {test.url} executed "
                f"the operation without validating the Authorization header or role permissions."
            )
        if test.criterion == TestCriterion.PERFORMANCE:
            return (
                f"SLA threshold breach. Response took {result.elapsed_ms:.1f}ms, which violates the "
                f"expected latency standard. Possible blocking I/O, unindexed query, or synchronous sleep."
            )
        if test.criterion == TestCriterion.WEB_VALIDATION:
            return f"Broken resource or asset contract on web interface. Target failed with: {reasons}"
        return f"Assertion failure: {reasons}"

    def _derive_expected_behavior(self, test: TestCase) -> str:
        assertion_descs = [a.description for a in test.assertions]
        return f"{test.description} Specifically: {'; '.join(assertion_descs)}"

    def _derive_actual_behavior(self, result: TestResult) -> str:
        status_info = f"HTTP {result.status_code}" if result.status_code else "No response status"
        snippet = f", Response: {result.response_body[:120]}..." if result.response_body else ""
        return f"Received {status_info} in {result.elapsed_ms:.1f}ms. Failures: {'; '.join(result.failed_assertions)}{snippet}"

    def _generate_reproduction_script(self, test: TestCase) -> str:
        """
        Generates an independent, copy-paste runnable Python reproduction script.
        It re-checks the same assertions and exits with code 1 while the bug is present.
        """
        checks = []
        for a in test.assertions:
            if a.assertion_type == AssertionType.BODY_MATCHES_SCHEMA:
                continue  # needs the full schema; covered by BugHunter itself
            checks.append({"type": a.assertion_type.value, "expected": a.expected_value, "description": a.description})

        payload_kw = ("data" if test.is_form_data else "json") if test.payload is not None else None
        # Credentials never go into the script; they are read from the environment at run time.
        headers = {k: v for k, v in test.headers.items() if not is_sensitive(k)}
        secret_headers = sorted(k for k in test.headers if is_sensitive(k))
        return f'''"""
Standalone Bug Reproduction Script for: {test.id}
Generated autonomously by BugHunter AI.

    pip install httpx
    python reproduce_bug.py     # exit code 1 = bug reproduced, 0 = fixed
{f"    # needs: {AUTH_ENV_VAR}='<header value>'" if secret_headers else ""}
"""

import json
import os
import sys
import time

import httpx

URL = {test.url!r}
METHOD = {test.method!r}
HEADERS = json.loads({json.dumps(headers)!r})
for name in {secret_headers!r}:
    HEADERS[name] = os.environ[{AUTH_ENV_VAR!r}]
PARAMS = json.loads({json.dumps(test.query_params)!r})
PAYLOAD = json.loads({json.dumps(test.payload)!r})
PAYLOAD_KIND = {payload_kw!r}
CHECKS = json.loads({json.dumps(checks)!r})


def failed_checks(resp, elapsed_ms):
    headers = {{k.lower() for k in resp.headers}}
    for check in CHECKS:
        kind, expected = check["type"], check["expected"]
        bad = (
            (kind == "STATUS_CODE_EQUALS" and resp.status_code != expected)
            or (kind == "STATUS_CODE_IN" and resp.status_code not in expected)
            or (kind == "NO_SERVER_ERROR" and 500 <= resp.status_code <= 599)
            or (kind == "RESPONSE_TIME_UNDER_MS" and elapsed_ms > expected)
            or (kind == "BODY_CONTAINS" and str(expected) not in resp.text)
            or (kind == "HEADER_EXISTS" and str(expected).lower() not in headers)
            or (kind == "NOT_REFLECTED_IN_HTML" and "text/html" in resp.headers.get("content-type", "")
                and str(expected) in resp.text)
        )
        if bad:
            yield check["description"]


kwargs = {{PAYLOAD_KIND: PAYLOAD}} if PAYLOAD_KIND else {{}}
print(f"[*] {{METHOD}} {{URL}}")
with httpx.Client(timeout=10.0, follow_redirects=False) as client:
    start = time.perf_counter()
    resp = client.request(METHOD, URL, headers=HEADERS, params=PARAMS or None, **kwargs)
    elapsed_ms = (time.perf_counter() - start) * 1000

print(f"[!] Status: {{resp.status_code}}  Time: {{elapsed_ms:.1f}}ms")
print(f"[!] Body: {{resp.text[:300]}}")
problems = list(failed_checks(resp, elapsed_ms))
for problem in problems:
    print(f"BUG REPRODUCED: {{problem}}")
if not problems:
    print("OK: the defect no longer reproduces.")
sys.exit(1 if problems else 0)
'''
