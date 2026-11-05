"""
Criteria-Driven Autonomous Test Generator.
Generates comprehensive test suites for APIs and Web Applications
according to user-specified criteria:
- FUNCTIONAL
- BOUNDARY
- SECURITY
- PERFORMANCE (SLA)
- WEB_VALIDATION
"""

from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin

from core.models import (
    AssertionType,
    EndpointSpec,
    FormFieldSpec,
    ParameterSpec,
    TargetType,
    TestAssertion,
    TestCase,
    TestCriterion,
    WebFormSpec,
    WebPageSpec,
)


class TestGenerator:
    """Generates targeted test cases based on parsed specs and selected criteria."""
    __test__ = False

    def __init__(
        self,
        base_url: str,
        criteria: Optional[Set[TestCriterion]] = None,
        sla_threshold_ms: float = 500.0
    ):
        self.base_url = base_url.rstrip("/")
        self.criteria = criteria or {
            TestCriterion.FUNCTIONAL,
            TestCriterion.BOUNDARY,
            TestCriterion.SECURITY,
            TestCriterion.PERFORMANCE,
            TestCriterion.WEB_VALIDATION,
        }
        self.sla_threshold_ms = sla_threshold_ms

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

        return tests

    def _gen_api_functional_tests(self, ep: EndpointSpec) -> List[TestCase]:
        tests: List[TestCase] = []
        path, query = self._resolve_path_and_query(ep, mode="happy")
        full_url = f"{self.base_url}{path}"
        payload = self._synthesize_payload(ep.request_body_schema, mode="valid")

        expected_status = [200, 201, 204]
        tests.append(
            TestCase(
                id=f"TC-API-FUNC-{ep.method}-{ep.path.replace('/', '_').strip('_')}",
                target_type=TargetType.API,
                criterion=TestCriterion.FUNCTIONAL,
                title=f"Happy Path: {ep.method} {ep.path}",
                description=f"Verify {ep.method} {ep.path} responds successfully with valid parameters.",
                method=ep.method,
                url=full_url,
                query_params=query,
                payload=payload,
                assertions=[
                    TestAssertion(
                        assertion_type=AssertionType.STATUS_CODE_IN,
                        expected_value=expected_status,
                        description=f"Status code should be in {expected_status}"
                    ),
                    TestAssertion(
                        assertion_type=AssertionType.NO_SERVER_ERROR,
                        expected_value=True,
                        description="Service must not return 5xx Internal Server Error"
                    )
                ],
                tags=["api", "functional", ep.method.lower()]
            )
        )
        return tests

    def _gen_api_boundary_tests(self, ep: EndpointSpec) -> List[TestCase]:
        tests: List[TestCase] = []

        # Path parameter boundary (non-existent/edge IDs)
        for param in ep.parameters:
            if param.in_location == "path":
                edge_path = ep.path.replace(f"{{{param.name}}}", "999999")
                tests.append(
                    TestCase(
                        id=f"TC-API-BOUND-NONEXIST-{ep.method}-{param.name}",
                        target_type=TargetType.API,
                        criterion=TestCriterion.BOUNDARY,
                        title=f"Boundary: Non-existent {param.name} on {ep.method} {ep.path}",
                        description=f"Non-existent {param.name} should return 404 or clean client error, never 500 crash.",
                        method=ep.method,
                        url=f"{self.base_url}{edge_path}",
                        assertions=[
                            TestAssertion(
                                assertion_type=AssertionType.STATUS_CODE_IN,
                                expected_value=[404, 400, 422],
                                description="Expected graceful 404 or 4xx for non-existent resource"
                            ),
                            TestAssertion(
                                assertion_type=AssertionType.NO_SERVER_ERROR,
                                expected_value=True,
                                description="Server must not crash with 500 on missing resource"
                            )
                        ],
                        tags=["api", "boundary", "error-handling"]
                    )
                )

        # Payload numeric boundary (Negative quantities/prices)
        if ep.request_body_schema and ep.method in ["POST", "PUT", "PATCH"]:
            negative_payload = self._synthesize_payload(ep.request_body_schema, mode="negative_numeric")
            if negative_payload:
                tests.append(
                    TestCase(
                        id=f"TC-API-BOUND-NEG-{ep.method}-{ep.path.replace('/', '_').strip('_')}",
                        target_type=TargetType.API,
                        criterion=TestCriterion.BOUNDARY,
                        title=f"Boundary: Negative numeric inputs on {ep.method} {ep.path}",
                        description="Negative quantities/prices must be rejected with 400/422 and not accepted or crash.",
                        method=ep.method,
                        url=f"{self.base_url}{ep.path}",
                        payload=negative_payload,
                        assertions=[
                            TestAssertion(
                                assertion_type=AssertionType.STATUS_CODE_IN,
                                expected_value=[400, 422],
                                description="Should reject negative quantities/values with 400 or 422"
                            ),
                            TestAssertion(
                                assertion_type=AssertionType.NO_SERVER_ERROR,
                                expected_value=True,
                                description="Server must not crash with 500"
                            )
                        ],
                        tags=["api", "boundary", "business-logic"]
                    )
                )

        return tests

    def _gen_api_security_tests(self, ep: EndpointSpec) -> List[TestCase]:
        tests: List[TestCase] = []

        # Destructive endpoints without authorization check
        if ep.method in ["DELETE", "PUT", "PATCH"]:
            path, query = self._resolve_path_and_query(ep, mode="happy")
            tests.append(
                TestCase(
                    id=f"TC-API-SEC-NOAUTH-{ep.method}-{ep.path.replace('/', '_').strip('_')}",
                    target_type=TargetType.API,
                    criterion=TestCriterion.SECURITY,
                    title=f"Security: Missing Authorization on {ep.method} {ep.path}",
                    description=f"Destructive operation {ep.method} {ep.path} must reject unauthenticated requests.",
                    method=ep.method,
                    url=f"{self.base_url}{path}",
                    query_params=query,
                    headers={},  # Missing Authorization header
                    assertions=[
                        TestAssertion(
                            assertion_type=AssertionType.STATUS_CODE_IN,
                            expected_value=[401, 403],
                            description="Destructive action without credentials must return 401 Unauthorized or 403 Forbidden"
                        )
                    ],
                    tags=["api", "security", "broken-auth"]
                )
            )

        return tests

    def _gen_api_performance_tests(self, ep: EndpointSpec) -> List[TestCase]:
        tests: List[TestCase] = []
        path, query = self._resolve_path_and_query(ep, mode="happy")
        tests.append(
            TestCase(
                id=f"TC-API-PERF-{ep.method}-{ep.path.replace('/', '_').strip('_')}",
                target_type=TargetType.API,
                criterion=TestCriterion.PERFORMANCE,
                title=f"Performance SLA: {ep.method} {ep.path}",
                description=f"Verify response latency is under SLA threshold of {self.sla_threshold_ms}ms.",
                method=ep.method,
                url=f"{self.base_url}{path}",
                query_params=query,
                assertions=[
                    TestAssertion(
                        assertion_type=AssertionType.RESPONSE_TIME_UNDER_MS,
                        expected_value=self.sla_threshold_ms,
                        description=f"Response time must be strictly under {self.sla_threshold_ms} ms"
                    )
                ],
                tags=["api", "performance", "sla"]
            )
        )
        return tests

    # -------------------------------------------------------------
    # Web & Website Test Generation
    # -------------------------------------------------------------
    def generate_web_tests(self, page_spec: WebPageSpec) -> List[TestCase]:
        tests: List[TestCase] = []

        if TestCriterion.WEB_VALIDATION in self.criteria or TestCriterion.FUNCTIONAL in self.criteria:
            # 1. Broken links crawler tests
            for i, link in enumerate(page_spec.links):
                tests.append(
                    TestCase(
                        id=f"TC-WEB-LINK-{i+1}",
                        target_type=TargetType.WEB,
                        criterion=TestCriterion.WEB_VALIDATION,
                        title=f"Web Link Integrity: {link.text[:30]} ({link.href})",
                        description=f"Verify link target {link.href} resolves without 404 or 500 error.",
                        method="GET",
                        url=link.href,
                        assertions=[
                            TestAssertion(
                                assertion_type=AssertionType.STATUS_CODE_IN,
                                expected_value=[200, 301, 302],
                                description="Link must resolve with successful HTTP status (not 404/500)"
                            ),
                            TestAssertion(
                                assertion_type=AssertionType.NO_SERVER_ERROR,
                                expected_value=True,
                                description="Link target must not produce 5xx server error"
                            )
                        ],
                        tags=["web", "links", "broken-link-crawler"]
                    )
                )

            # 2. Asset integrity tests
            for i, asset in enumerate(page_spec.assets):
                tests.append(
                    TestCase(
                        id=f"TC-WEB-ASSET-{i+1}",
                        target_type=TargetType.WEB,
                        criterion=TestCriterion.WEB_VALIDATION,
                        title=f"Web Asset Integrity: {asset.tag_name} ({asset.href})",
                        description=f"Verify {asset.tag_name} resource loads successfully (not 404).",
                        method="GET",
                        url=asset.href,
                        assertions=[
                            TestAssertion(
                                assertion_type=AssertionType.STATUS_CODE_EQUALS,
                                expected_value=200,
                                description="Asset must exist and return HTTP 200"
                            )
                        ],
                        tags=["web", "assets", asset.tag_name]
                    )
                )

        if TestCriterion.BOUNDARY in self.criteria or TestCriterion.WEB_VALIDATION in self.criteria:
            # 3. Form submission boundary tests
            for i, form in enumerate(page_spec.forms):
                # Test with empty/malformed inputs
                malformed_form_data = {
                    field.name: "invalid_no_at_sign" if "email" in field.name.lower() else ""
                    for field in form.fields
                }
                tests.append(
                    TestCase(
                        id=f"TC-WEB-FORM-VALIDATION-{i+1}",
                        target_type=TargetType.WEB,
                        criterion=TestCriterion.BOUNDARY,
                        title=f"Web Form Validation: {form.action} (Malformed Inputs)",
                        description="Form submission with malformed/missing fields must reject gracefully, never crash with 500.",
                        method=form.method,
                        url=form.action,
                        payload=malformed_form_data,
                        is_form_data=True,
                        assertions=[
                            TestAssertion(
                                assertion_type=AssertionType.NO_SERVER_ERROR,
                                expected_value=True,
                                description="Server must not crash with 500 on malformed form input"
                            ),
                            TestAssertion(
                                assertion_type=AssertionType.STATUS_CODE_IN,
                                expected_value=[200, 400, 422],
                                description="Response should be 400/422 validation failure or handled 200 with error message"
                            )
                        ],
                        tags=["web", "forms", "boundary"]
                    )
                )

        return tests

    # -------------------------------------------------------------
    # Helper Payload Synthesizers
    # -------------------------------------------------------------
    def _resolve_path_and_query(self, ep: EndpointSpec, mode: str = "happy"):
        path = ep.path
        query = {}
        for p in ep.parameters:
            if p.in_location == "path":
                val = 1 if p.param_type == "integer" else "demo"
                path = path.replace(f"{{{p.name}}}", str(val))
            elif p.in_location == "query":
                val = p.default if p.default is not None else ("test" if p.param_type == "string" else 1)
                query[p.name] = val
        return path, query

    def _synthesize_payload(self, schema: Optional[Dict[str, Any]], mode: str = "valid") -> Optional[Dict[str, Any]]:
        if not schema or not isinstance(schema, dict):
            return None

        props = schema.get("properties", {})
        payload = {}

        for prop_name, prop_spec in props.items():
            p_type = prop_spec.get("type", "string")

            if mode == "valid":
                if p_type == "integer":
                    payload[prop_name] = 1
                elif p_type == "number":
                    payload[prop_name] = 19.99
                elif p_type == "boolean":
                    payload[prop_name] = True
                else:
                    payload[prop_name] = f"Sample {prop_name}"

            elif mode == "negative_numeric":
                if p_type in ["integer", "number"]:
                    payload[prop_name] = -5 if p_type == "integer" else -15.5
                else:
                    payload[prop_name] = f"Sample {prop_name}"

        return payload
