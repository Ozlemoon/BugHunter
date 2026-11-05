"""
Bug Analysis & Evidence Collection Engine.
Isolates failed tests, conducts root cause analysis, and synthesizes minimal,
independently reproducible Python evidence scripts.
"""

import json
from typing import List, Optional
from core.models import BugEvidence, TestCase, TestCriterion, TestResult


class BugAnalyzer:
    """Analyzes test failures and generates structured evidence and reproduction scripts."""

    def analyze_failure(self, test: TestCase, result: TestResult) -> BugEvidence:
        """Constructs BugEvidence from a failed test and execution result."""
        bug_id = f"BUG-{test.id.replace('TC-', '')}"
        severity = self._determine_severity(test, result)
        root_cause = self._hypothesize_root_cause(test, result)
        expected_behavior = self._derive_expected_behavior(test)
        actual_behavior = self._derive_actual_behavior(result)
        reproduction_code = self._generate_reproduction_script(test)

        return BugEvidence(
            bug_id=bug_id,
            title=f"Defect: {test.title}",
            target_type=test.target_type,
            criterion=test.criterion,
            target_url=test.url,
            method=test.method,
            payload=test.payload,
            expected_behavior=expected_behavior,
            actual_behavior=actual_behavior,
            reproduction_code=reproduction_code,
            root_cause_hypothesis=root_cause,
            severity=severity,
        )

    def _determine_severity(self, test: TestCase, result: TestResult) -> str:
        # 500 errors or broken auth are critical
        if result.status_code and 500 <= result.status_code <= 599:
            return "CRITICAL"
        if test.criterion == TestCriterion.SECURITY:
            return "CRITICAL"
        if test.criterion == TestCriterion.BOUNDARY:
            return "HIGH"
        if test.criterion == TestCriterion.PERFORMANCE:
            return "MEDIUM"
        if test.criterion == TestCriterion.WEB_VALIDATION:
            if result.status_code == 404:
                return "MEDIUM"
            return "HIGH"
        return "MEDIUM"

    def _hypothesize_root_cause(self, test: TestCase, result: TestResult) -> str:
        reasons = "; ".join(result.failed_assertions)

        if result.status_code and 500 <= result.status_code <= 599:
            return (
                f"Unhandled server exception during {test.method} {test.url}. "
                f"The backend likely lacks input sanitization or safe dictionary/list indexing, "
                f"causing a raw 500 Internal Server Error: {reasons}"
            )

        if test.criterion == TestCriterion.BOUNDARY:
            return (
                f"Missing boundary constraint validation. The service accepted invalid/negative input "
                f"without returning HTTP 400 or 422 Bad Request: {reasons}"
            )

        if test.criterion == TestCriterion.SECURITY:
            return (
                f"Missing or unenforced access control. Endpoint {test.method} {test.url} executed "
                f"a destructive operation without validating the Authorization header or role permissions."
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
        """Generates an independent, copy-paste runnable Python reproduction script."""
        headers_literal = json.dumps(test.headers, indent=4)
        params_literal = json.dumps(test.query_params, indent=4)
        payload_literal = json.dumps(test.payload, indent=4) if test.payload is not None else "None"

        payload_arg = "data=payload" if test.is_form_data else "json=payload"

        return f'''"""
Standalone Bug Reproduction Script for: {test.id}
Generated autonomously by BugHunter AI.
Run independently to reproduce the defect:
    python reproduce_bug.py
"""

import httpx

url = "{test.url}"
method = "{test.method}"
headers = {headers_literal}
params = {params_literal}
payload = {payload_literal}

print(f"[*] Sending {{method}} request to {{url}}...")
with httpx.Client(timeout=10.0, follow_redirects=False) as client:
    resp = client.request(
        method=method,
        url=url,
        headers=headers,
        params=params or None,
        {payload_arg if test.payload is not None else ""}
    )
    print(f"[!] Response Status: {{resp.status_code}}")
    print(f"[!] Response Time: {{resp.elapsed.total_seconds() * 1000:.1f}}ms")
    print(f"[!] Response Body: {{resp.text[:300]}}")

    if 500 <= resp.status_code <= 599:
        print("❌ CRITICAL BUG CONFIRMED: Server crashed with 5xx error!")
    elif resp.status_code == 200 and "NEG" in "{test.id}":
        print("❌ BUSINESS LOGIC BUG CONFIRMED: Negative value was improperly accepted!")
    elif resp.status_code == 200 and "NOAUTH" in "{test.id}":
        print("❌ SECURITY VULNERABILITY CONFIRMED: Unauthorized destructive action allowed!")
    elif resp.status_code == 404:
        print("❌ BROKEN LINK CONFIRMED: 404 Not Found!")
    else:
        print("ℹ️ Finished execution.")
'''
