"""
Criteria-Driven Autonomous Test Generator.

Turns parsed API / web specifications into executable TestCase objects for the
selected criteria (FUNCTIONAL, BOUNDARY, SECURITY, PERFORMANCE, WEB_VALIDATION).

Design rules that keep results trustworthy against real, stateful targets:
- Tests are ordered safe-first: read-only requests, then writes, then deletes,
  so a DELETE can never make an earlier GET fail.
- Performance probes only use safe methods; they never create or delete data.
- Unauthenticated destructive probes target a *sibling* resource id, so a
  successful unauthorised delete does not contaminate the functional checks.
- Spec-provided examples/defaults are preferred over invented values.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.parse import urlparse

from core.models import (
    AssertionType,
    EndpointSpec,
    FormFieldSpec,
    ParameterSpec,
    Severity,
    TargetType,
    TestAssertion,
    TestCase,
    TestCriterion,
    WebFormSpec,
    WebPageSpec,
)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
DESTRUCTIVE_METHODS = {"DELETE", "PUT", "PATCH"}
SUCCESS_STATUSES = [200, 201, 202, 204]
CLIENT_ERROR_STATUSES = [400, 404, 422]
VALIDATION_ERROR_STATUSES = [400, 422]
AUTH_ERROR_STATUSES = [401, 403]

XSS_PROBE = "\"><script>bughunter_xss_probe()</script>"
INVALID_TOKEN = "Bearer bughunter-invalid-token"
SECURITY_HEADERS = ["X-Content-Type-Options", "X-Frame-Options", "Content-Security-Policy"]

ALL_CRITERIA = {
    TestCriterion.FUNCTIONAL,
    TestCriterion.BOUNDARY,
    TestCriterion.SECURITY,
    TestCriterion.PERFORMANCE,
    TestCriterion.WEB_VALIDATION,
}


def _no_server_error(description: str = "Service must not return 5xx Internal Server Error") -> TestAssertion:
    return TestAssertion(assertion_type=AssertionType.NO_SERVER_ERROR, expected_value=True, description=description)


def _status_in(statuses: List[int], description: str) -> TestAssertion:
    return TestAssertion(assertion_type=AssertionType.STATUS_CODE_IN, expected_value=statuses, description=description)


def _slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "-", path).strip("-") or "root"


class TestGenerator:
    """Generates targeted test cases based on parsed specs and selected criteria."""
    __test__ = False

    def __init__(
        self,
        base_url: str,
        criteria: Optional[Set[TestCriterion]] = None,
        sla_threshold_ms: float = 500.0,
        auth_headers: Optional[Dict[str, str]] = None,
        same_origin_only: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.criteria = set(criteria) if criteria else set(ALL_CRITERIA)
        self.sla_threshold_ms = sla_threshold_ms
        self.auth_headers = dict(auth_headers or {})
        self.same_origin_only = same_origin_only
        self._ids: Set[str] = set()

    # -------------------------------------------------------------
    # Shared helpers
    # -------------------------------------------------------------
    def _unique_id(self, candidate: str) -> str:
        test_id, counter = candidate, 2
        while test_id in self._ids:
            test_id = f"{candidate}-{counter}"
            counter += 1
        self._ids.add(test_id)
        return test_id

    def _api_test(self, kind: str, ep: EndpointSpec, criterion: TestCriterion, **fields: Any) -> TestCase:
        return TestCase(
            id=self._unique_id(f"TC-API-{kind}-{ep.method}-{_slug(ep.path)}"),
            target_type=TargetType.API,
            criterion=criterion,
            method=ep.method,
            endpoint=ep.path,
            **fields,
        )

    @staticmethod
    def _order_key(test: TestCase) -> tuple:
        method = test.method.upper()
        phase = 0 if method in SAFE_METHODS else (2 if method == "DELETE" else 1)
        # Inside the destructive phase, run the negative probes before the real delete.
        within = {TestCriterion.SECURITY: 0, TestCriterion.BOUNDARY: 1}.get(test.criterion, 2) if phase == 2 else 0
        return phase, within

    def _ordered(self, tests: Iterable[TestCase]) -> List[TestCase]:
        return sorted(tests, key=self._order_key)  # stable: keeps spec order inside a phase

    # -------------------------------------------------------------
    # API Test Generation
    # -------------------------------------------------------------
    def generate_api_tests(self, endpoints: List[EndpointSpec]) -> List[TestCase]:
        tests: List[TestCase] = []
        for ep in endpoints:
            if TestCriterion.FUNCTIONAL in self.criteria:
                tests.extend(self._gen_api_functional_tests(ep))
            if TestCriterion.BOUNDARY in self.criteria:
                tests.extend(self._gen_api_boundary_tests(ep))
            if TestCriterion.SECURITY in self.criteria:
                tests.extend(self._gen_api_security_tests(ep))
            if TestCriterion.PERFORMANCE in self.criteria:
                tests.extend(self._gen_api_performance_tests(ep))
        return self._ordered(tests)

    def _gen_api_functional_tests(self, ep: EndpointSpec) -> List[TestCase]:
        path, query = self._resolve_path_and_query(ep)
        assertions = [
            _status_in(SUCCESS_STATUSES, f"Status code should be in {SUCCESS_STATUSES}"),
            _no_server_error(),
        ]
        if ep.success_response_schema:
            assertions.append(TestAssertion(
                assertion_type=AssertionType.BODY_MATCHES_SCHEMA,
                expected_value=ep.success_response_schema,
                description="Response body must match the documented response schema",
            ))
        return [self._api_test(
            "FUNC", ep, TestCriterion.FUNCTIONAL,
            title=f"Happy Path: {ep.method} {ep.path}",
            description=f"Verify {ep.method} {ep.path} responds successfully with valid parameters.",
            url=f"{self.base_url}{path}",
            headers=dict(self.auth_headers),
            query_params=query,
            payload=self._synthesize_payload(ep.request_body_schema, mode="valid"),
            assertions=assertions,
            tags=["api", "functional", ep.method.lower()],
        )]

    def _gen_api_boundary_tests(self, ep: EndpointSpec) -> List[TestCase]:
        tests: List[TestCase] = []
        valid_payload = self._synthesize_payload(ep.request_body_schema, mode="valid")

        # Non-existent resource ids must produce a clean 4xx, never a crash.
        for param in ep.parameters:
            if param.in_location != "path":
                continue
            missing = "999999" if param.param_type in ("integer", "number") else "bughunter-missing"
            path, query = self._resolve_path_and_query(ep, overrides={param.name: missing})
            tests.append(self._api_test(
                f"BOUND-NONEXIST-{param.name}", ep, TestCriterion.BOUNDARY,
                title=f"Boundary: Non-existent {param.name} on {ep.method} {ep.path}",
                description=f"Non-existent {param.name} should return 404 or clean client error, never 500 crash.",
                url=f"{self.base_url}{path}",
                headers=dict(self.auth_headers),
                query_params=query,
                payload=valid_payload,
                assertions=[
                    _status_in(CLIENT_ERROR_STATUSES, "Expected graceful 404 or 4xx for non-existent resource"),
                    _no_server_error("Server must not crash with 500 on missing resource"),
                ],
                tags=["api", "boundary", "error-handling"],
            ))

        if not ep.request_body_schema or ep.method not in ("POST", "PUT", "PATCH"):
            return tests
        path, query = self._resolve_path_and_query(ep)
        common = dict(url=f"{self.base_url}{path}", headers=dict(self.auth_headers), query_params=query)

        negative = self._synthesize_payload(ep.request_body_schema, mode="negative_numeric")
        if negative and negative != valid_payload:
            tests.append(self._api_test(
                "BOUND-NEG", ep, TestCriterion.BOUNDARY,
                title=f"Boundary: Negative numeric inputs on {ep.method} {ep.path}",
                description="Negative quantities/prices must be rejected with 400/422 and not accepted or crash.",
                payload=negative,
                assertions=[
                    _status_in(VALIDATION_ERROR_STATUSES, "Should reject negative quantities/values with 400 or 422"),
                    _no_server_error("Server must not crash with 500"),
                ],
                tags=["api", "boundary", "business-logic", "negative-values"],
                **common,
            ))

        if ep.request_body_schema.get("required"):
            tests.append(self._api_test(
                "BOUND-MISSING", ep, TestCriterion.BOUNDARY,
                title=f"Boundary: Missing required fields on {ep.method} {ep.path}",
                description="A body without the documented required fields must be rejected with 400/422.",
                payload={},
                assertions=[
                    _status_in(VALIDATION_ERROR_STATUSES, "Missing required fields must be rejected with 400 or 422"),
                    _no_server_error(),
                ],
                tags=["api", "boundary", "missing-required"],
                **common,
            ))

        wrong_types = self._synthesize_payload(ep.request_body_schema, mode="wrong_type")
        if wrong_types and wrong_types != valid_payload:
            tests.append(self._api_test(
                "BOUND-TYPE", ep, TestCriterion.BOUNDARY,
                title=f"Boundary: Wrong field types on {ep.method} {ep.path}",
                description="Text sent in numeric/boolean fields must be rejected with 400/422, never crash.",
                payload=wrong_types,
                assertions=[
                    _status_in(VALIDATION_ERROR_STATUSES, "Wrongly typed fields must be rejected with 400 or 422"),
                    _no_server_error(),
                ],
                tags=["api", "boundary", "wrong-type"],
                **common,
            ))
        return tests

    def _gen_api_security_tests(self, ep: EndpointSpec) -> List[TestCase]:
        tests: List[TestCase] = []
        if ep.method not in DESTRUCTIVE_METHODS and not ep.requires_auth:
            return tests

        payload = self._synthesize_payload(ep.request_body_schema, mode="valid")
        destructive = ep.method in DESTRUCTIVE_METHODS
        path, query = self._resolve_path_and_query(ep, sibling_offset=1 if destructive else 0)
        tests.append(self._api_test(
            "SEC-NOAUTH", ep, TestCriterion.SECURITY,
            title=f"Security: Missing Authorization on {ep.method} {ep.path}",
            description=f"{ep.method} {ep.path} must reject requests that carry no credentials.",
            url=f"{self.base_url}{path}",
            query_params=query,
            payload=payload,
            headers={},  # deliberately unauthenticated
            assertions=[_status_in(AUTH_ERROR_STATUSES, "Request without credentials must return 401 or 403")],
            tags=["api", "security", "broken-auth"],
        ))

        if self.auth_headers:
            path, query = self._resolve_path_and_query(ep, sibling_offset=2 if destructive else 0)
            forged = {name: INVALID_TOKEN if name.lower() == "authorization" else "bughunter-invalid"
                      for name in self.auth_headers}
            tests.append(self._api_test(
                "SEC-BADTOKEN", ep, TestCriterion.SECURITY,
                title=f"Security: Invalid credentials on {ep.method} {ep.path}",
                description=f"{ep.method} {ep.path} must reject forged or expired credentials.",
                url=f"{self.base_url}{path}",
                query_params=query,
                payload=payload,
                headers=forged,
                assertions=[_status_in(AUTH_ERROR_STATUSES, "Invalid credentials must return 401 or 403")],
                tags=["api", "security", "broken-auth", "invalid-token"],
            ))
        return tests

    def _gen_api_performance_tests(self, ep: EndpointSpec) -> List[TestCase]:
        if ep.method not in SAFE_METHODS:
            return []  # never create/delete data just to time it
        path, query = self._resolve_path_and_query(ep)
        return [self._api_test(
            "PERF", ep, TestCriterion.PERFORMANCE,
            title=f"Performance SLA: {ep.method} {ep.path}",
            description=f"Verify response latency is under SLA threshold of {self.sla_threshold_ms}ms.",
            url=f"{self.base_url}{path}",
            headers=dict(self.auth_headers),
            query_params=query,
            assertions=[TestAssertion(
                assertion_type=AssertionType.RESPONSE_TIME_UNDER_MS,
                expected_value=self.sla_threshold_ms,
                description=f"Response time must be strictly under {self.sla_threshold_ms} ms",
            )],
            tags=["api", "performance", "sla"],
        )]

    # -------------------------------------------------------------
    # Web & Website Test Generation
    # -------------------------------------------------------------
    def generate_web_tests(self, page_spec: WebPageSpec) -> List[TestCase]:
        tests: List[TestCase] = []
        page_path = urlparse(page_spec.url).path or "/"

        if TestCriterion.WEB_VALIDATION in self.criteria or TestCriterion.FUNCTIONAL in self.criteria:
            for i, link in enumerate(self._in_scope(page_spec.links, page_spec.url), 1):
                tests.append(TestCase(
                    id=self._unique_id(f"TC-WEB-LINK-{i}"),
                    target_type=TargetType.WEB,
                    criterion=TestCriterion.WEB_VALIDATION,
                    title=f"Web Link Integrity: {link.text[:30]} ({link.href})",
                    description=f"Verify link target {link.href} resolves without 404 or 500 error.",
                    method="GET",
                    url=link.href,
                    endpoint=urlparse(link.href).path or "/",
                    assertions=[
                        _status_in([200, 301, 302, 303, 307, 308],
                                   "Link must resolve with successful HTTP status (not 404/500)"),
                        _no_server_error("Link target must not produce 5xx server error"),
                    ],
                    tags=["web", "links", "broken-link-crawler"],
                ))
            for i, asset in enumerate(self._in_scope(page_spec.assets, page_spec.url), 1):
                tests.append(TestCase(
                    id=self._unique_id(f"TC-WEB-ASSET-{i}"),
                    target_type=TargetType.WEB,
                    criterion=TestCriterion.WEB_VALIDATION,
                    title=f"Web Asset Integrity: {asset.tag_name} ({asset.href})",
                    description=f"Verify {asset.tag_name} resource loads successfully (not 404).",
                    method="GET",
                    url=asset.href,
                    endpoint=urlparse(asset.href).path or "/",
                    assertions=[TestAssertion(
                        assertion_type=AssertionType.STATUS_CODE_EQUALS,
                        expected_value=200,
                        description="Asset must exist and return HTTP 200",
                    )],
                    tags=["web", "assets", asset.tag_name],
                ))

        for i, form in enumerate(page_spec.forms, 1):
            if TestCriterion.FUNCTIONAL in self.criteria:
                tests.append(self._form_test(
                    form, i, "HAPPY", TestCriterion.FUNCTIONAL,
                    title=f"Web Form Submission: {form.action} (Valid Inputs)",
                    description="Submitting the form with realistic valid values must succeed without errors.",
                    values={f.name: self._valid_form_value(f) for f in form.fields},
                    assertions=[
                        _status_in([200, 201, 302, 303], "Valid submission must succeed or redirect"),
                        _no_server_error("Valid form submission must not crash the server"),
                    ],
                    tags=["web", "forms", "functional"],
                ))
            if TestCriterion.BOUNDARY in self.criteria or TestCriterion.WEB_VALIDATION in self.criteria:
                tests.append(self._form_test(
                    form, i, "VALIDATION", TestCriterion.BOUNDARY,
                    title=f"Web Form Validation: {form.action} (Malformed Inputs)",
                    description="Form submission with malformed/missing fields must reject gracefully, never crash with 500.",
                    values={f.name: self._malformed_form_value(f) for f in form.fields},
                    assertions=[
                        _no_server_error("Server must not crash with 500 on malformed form input"),
                        _status_in([200, 302, 303, 400, 422],
                                   "Response should be 400/422 validation failure or handled 200 with error message"),
                    ],
                    tags=["web", "forms", "boundary"],
                ))
            text_fields = [f for f in form.fields if self._is_free_text(f)]
            if TestCriterion.SECURITY in self.criteria and text_fields:
                values = {f.name: self._valid_form_value(f) for f in form.fields}
                values.update({f.name: XSS_PROBE for f in text_fields})
                tests.append(self._form_test(
                    form, i, "XSS", TestCriterion.SECURITY,
                    title=f"Reflected XSS: {form.action}",
                    description="User input must be HTML-escaped before it is rendered back into a page.",
                    values=values,
                    assertions=[TestAssertion(
                        assertion_type=AssertionType.NOT_REFLECTED_IN_HTML,
                        expected_value=XSS_PROBE,
                        description="Script probe must not appear unescaped in an HTML response",
                    )],
                    tags=["web", "forms", "security", "xss"],
                    severity=Severity.HIGH,
                ))

        if TestCriterion.SECURITY in self.criteria:
            tests.append(TestCase(
                id=self._unique_id("TC-WEB-SEC-HEADERS"),
                target_type=TargetType.WEB,
                criterion=TestCriterion.SECURITY,
                title=f"Security Headers: {page_path}",
                description="Pages should send standard hardening headers (nosniff, framing and content policies).",
                method="GET",
                url=page_spec.url,
                endpoint=page_path,
                assertions=[TestAssertion(
                    assertion_type=AssertionType.HEADER_EXISTS,
                    expected_value=header,
                    description=f"Response must include the {header} header",
                ) for header in SECURITY_HEADERS],
                tags=["web", "security", "security-headers"],
                severity=Severity.LOW,
            ))

        if TestCriterion.PERFORMANCE in self.criteria:
            tests.append(TestCase(
                id=self._unique_id("TC-WEB-PERF-PAGE"),
                target_type=TargetType.WEB,
                criterion=TestCriterion.PERFORMANCE,
                title=f"Page Load SLA: {page_path}",
                description=f"Verify the page responds under the SLA threshold of {self.sla_threshold_ms}ms.",
                method="GET",
                url=page_spec.url,
                endpoint=page_path,
                assertions=[TestAssertion(
                    assertion_type=AssertionType.RESPONSE_TIME_UNDER_MS,
                    expected_value=self.sla_threshold_ms,
                    description=f"Response time must be strictly under {self.sla_threshold_ms} ms",
                )],
                tags=["web", "performance", "sla"],
            ))

        return self._ordered(tests)

    def _form_test(self, form: WebFormSpec, index: int, kind: str, criterion: TestCriterion,
                   values: Dict[str, str], **fields: Any) -> TestCase:
        is_get = form.method.upper() == "GET"
        return TestCase(
            id=self._unique_id(f"TC-WEB-FORM-{kind}-{index}"),
            target_type=TargetType.WEB,
            criterion=criterion,
            method=form.method.upper(),
            url=form.action,
            endpoint=urlparse(form.action).path or "/",
            query_params=values if is_get else {},
            payload=None if is_get else values,
            is_form_data=not is_get,
            **fields,
        )

    def _in_scope(self, links, page_url: str):
        if not self.same_origin_only:
            return list(links)
        origin = urlparse(page_url).netloc
        return [link for link in links if urlparse(link.href).netloc == origin]

    @staticmethod
    def _is_free_text(field: FormFieldSpec) -> bool:
        return field.field_type in ("text", "search", "textarea") and "email" not in field.name.lower()

    @staticmethod
    def _valid_form_value(field: FormFieldSpec) -> str:
        name, kind = field.name.lower(), field.field_type
        if kind == "email" or "email" in name:
            return "tester@example.com"
        if kind == "number":
            return "1"
        if kind == "url":
            return "https://example.com"
        if kind == "tel" or "phone" in name:
            return "+905551112233"
        if kind in ("hidden", "submit", "checkbox", "radio") and field.value is not None:
            return field.value
        return "BugHunter test"

    @staticmethod
    def _malformed_form_value(field: FormFieldSpec) -> str:
        if field.field_type == "email" or "email" in field.name.lower():
            return "invalid_no_at_sign"
        if field.field_type == "number":
            return "not-a-number"
        return ""

    # -------------------------------------------------------------
    # Helper Payload Synthesizers
    # -------------------------------------------------------------
    @staticmethod
    def _param_value(param: ParameterSpec, sibling_offset: int = 0) -> Any:
        value = param.example if param.example is not None else param.default
        if param.param_type in ("integer", "number"):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                value = 1
            return value + sibling_offset
        if value is None:
            value = "test" if param.in_location == "query" else "demo"
        return f"{value}-{sibling_offset}" if sibling_offset else value

    def _resolve_path_and_query(
        self, ep: EndpointSpec, overrides: Optional[Dict[str, Any]] = None, sibling_offset: int = 0
    ):
        overrides = overrides or {}
        path = ep.path
        query: Dict[str, Any] = {}
        for p in ep.parameters:
            if p.in_location == "path":
                value = overrides.get(p.name, self._param_value(p, sibling_offset))
                path = path.replace(f"{{{p.name}}}", str(value))
            elif p.in_location == "query" and (p.required or p.default is not None or p.example is not None):
                query[p.name] = overrides.get(p.name, self._param_value(p))
        return path, query

    def _synthesize_payload(self, schema: Optional[Dict[str, Any]], mode: str = "valid") -> Optional[Dict[str, Any]]:
        if not schema or not isinstance(schema, dict):
            return None
        props = schema.get("properties", {})
        if not props:
            return None

        payload: Dict[str, Any] = {}
        for prop_name, prop_spec in props.items():
            prop_spec = prop_spec if isinstance(prop_spec, dict) else {}
            p_type = prop_spec.get("type") or next(
                (o.get("type") for o in prop_spec.get("anyOf", []) if isinstance(o, dict) and o.get("type") != "null"),
                "string",
            )
            numeric = p_type in ("integer", "number")
            valid = self._valid_value(prop_name, prop_spec, p_type)
            # Identifiers stay valid so the probe reaches the business rule, not a 404.
            is_identifier = prop_name.lower() == "id" or prop_name.lower().endswith("_id")

            if mode == "negative_numeric" and numeric and not is_identifier:
                payload[prop_name] = -5 if p_type == "integer" else -15.5
            elif mode == "wrong_type" and (numeric or p_type == "boolean"):
                payload[prop_name] = "not-a-number"
            else:
                payload[prop_name] = valid
        return payload

    @staticmethod
    def _valid_value(name: str, spec: Dict[str, Any], p_type: str) -> Any:
        for key in ("example", "default"):
            if key in spec:
                return spec[key]
        if spec.get("examples"):
            return spec["examples"][0]
        if spec.get("enum"):
            return spec["enum"][0]
        if p_type == "integer":
            return max(1, spec.get("minimum", 1))
        if p_type == "number":
            return 19.99
        if p_type == "boolean":
            return True
        if p_type == "array":
            return []
        if p_type == "object":
            return {}
        if spec.get("format") == "email" or "email" in name.lower():
            return "tester@example.com"
        return f"Sample {name}"
