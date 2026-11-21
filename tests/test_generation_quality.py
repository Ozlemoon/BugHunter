"""
End-to-end quality checks for test generation, execution and fix rules
against the deliberately buggy playground targets (run in-process).
"""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from core import schema
from core.analyzer import BugAnalyzer
from core.generator import XSS_PROBE, TestGenerator
from core.models import AssertionType, TestAssertion
from core.patcher import CodePatcher, HumanApprovalGate
from core.runner import TestRunner
from core.targets.api_target import ApiTargetParser, SpecError
from core.targets.web_target import WebTargetParser
from tests.helpers import API_BASE, AUTH, WEB_BASE, load_app


def api_suite(app, auth=AUTH, sla=500.0):
    parser = ApiTargetParser(f"{API_BASE}/openapi.json")
    parser.raw_spec = app.openapi()
    return TestGenerator(API_BASE, sla_threshold_ms=sla, auth_headers=auth).generate_api_tests(parser.parse())


def web_suite(client):
    spec = WebTargetParser(f"{WEB_BASE}/", client=client).crawl(max_pages=5)
    return TestGenerator(WEB_BASE).generate_web_tests(spec)


def failing(tests, results):
    return {t.id: (t, r) for t, r in zip(tests, results) if not r.passed}


EXPECTED_API_FAILURES = {
    "TC-API-PERF-GET-products-heavy-search": None,
    "TC-API-BOUND-NONEXIST-user_id-GET-users-user_id-profile": "missing-profile-returns-404",
    "TC-API-BOUND-NEG-POST-products": "product-values-must-be-non-negative",
    "TC-API-BOUND-NEG-POST-orders": "order-quantity-must-be-positive",
    "TC-API-SEC-NOAUTH-DELETE-products-product_id": "delete-product-requires-admin",
    "TC-API-SEC-BADTOKEN-DELETE-products-product_id": "delete-product-requires-admin",
}

EXPECTED_WEB_FAILURES = {
    "TC-WEB-LINK-3": None,                        # /legacy-api-docs -> 404
    "TC-WEB-ASSET-1": None,                       # missing logo -> 404
    "TC-WEB-FORM-XSS-1": "escape-search-query",
    "TC-WEB-SEC-HEADERS": "add-security-headers-middleware",
    "TC-WEB-FORM-VALIDATION-2": "contact-email-must-contain-at",
}


def apply_all_fixes(failures, target: Path):
    gate = HumanApprovalGate()
    patcher = CodePatcher(approval_gate=gate)
    applied = set()
    for test, result in failures.values():
        proposal = patcher.generate_fix(BugAnalyzer().analyze_failure(test, result), str(target))
        if proposal.patch_available and proposal.rule not in applied:
            gate.approve_fix(proposal.fix_id)
            gate.apply_patch(proposal.fix_id, create_backup=False)
            applied.add(proposal.rule)
    return applied


def test_api_suite_finds_exactly_the_planted_bugs_with_the_right_fix(api_copy):
    app = load_app(api_copy)
    tests = api_suite(app)
    results = TestRunner().run_suite(tests, client=TestClient(app, raise_server_exceptions=False))
    failures = failing(tests, results)

    assert set(failures) == set(EXPECTED_API_FAILURES)
    patcher = CodePatcher()
    for test_id, (test, result) in failures.items():
        proposal = patcher.generate_fix(BugAnalyzer().analyze_failure(test, result), str(api_copy))
        assert proposal.rule == EXPECTED_API_FAILURES[test_id], test_id


def test_api_fixes_remove_every_patchable_bug_without_regressions(api_copy):
    app = load_app(api_copy)
    tests = api_suite(app)
    before = failing(tests, TestRunner().run_suite(tests, client=TestClient(app, raise_server_exceptions=False)))
    apply_all_fixes(before, api_copy)

    patched_app = load_app(api_copy)
    after = TestRunner().run_suite(tests, client=TestClient(patched_app, raise_server_exceptions=False))
    still_failing = {t.id for t, r in zip(tests, after) if not r.passed}
    assert still_failing == {"TC-API-PERF-GET-products-heavy-search"}  # SLA needs a human decision


def test_web_suite_crawls_forms_and_fixes_apply(web_copy):
    app = load_app(web_copy)
    client = TestClient(app, raise_server_exceptions=False, base_url=WEB_BASE)
    tests = web_suite(client)
    failures = failing(tests, TestRunner().run_suite(tests, client=client))
    assert set(failures) == set(EXPECTED_WEB_FAILURES)

    apply_all_fixes(failures, web_copy)
    patched = TestClient(load_app(web_copy), raise_server_exceptions=False, base_url=WEB_BASE)
    after = TestRunner().run_suite(tests, client=patched)
    assert {t.id for t, r in zip(tests, after) if not r.passed} == {"TC-WEB-LINK-3", "TC-WEB-ASSET-1"}


def test_tests_are_ordered_safe_first_and_ids_are_unique(api_copy):
    tests = api_suite(load_app(api_copy))
    phases = [0 if t.method == "GET" else 2 if t.method == "DELETE" else 1 for t in tests]
    assert phases == sorted(phases)
    assert len({t.id for t in tests}) == len(tests)
    deletes = [t for t in tests if t.method == "DELETE"]
    assert deletes[-1].criterion.value == "FUNCTIONAL"  # the real delete runs last


def test_performance_probes_never_mutate_data(api_copy):
    tests = api_suite(load_app(api_copy))
    assert all(t.method == "GET" for t in tests if t.criterion.value == "PERFORMANCE")


def test_security_probes_use_sibling_ids_and_no_credentials(api_copy):
    tests = {t.id: t for t in api_suite(load_app(api_copy))}
    no_auth = tests["TC-API-SEC-NOAUTH-DELETE-products-product_id"]
    functional = tests["TC-API-FUNC-DELETE-products-product_id"]
    assert no_auth.headers == {} and no_auth.url.endswith("/products/2")
    assert functional.headers == AUTH and functional.url.endswith("/products/1")


def test_parser_uses_spec_examples_and_servers(api_copy):
    parser = ApiTargetParser("https://shop.example/docs/openapi.json")
    parser.raw_spec = {**load_app(api_copy).openapi(), "servers": [{"url": "/v1"}]}
    endpoints = {(e.method, e.path): e for e in parser.parse()}
    profile = endpoints[("GET", "/users/{user_id}/profile")]
    assert profile.parameters[0].example == 101
    assert endpoints[("POST", "/orders")].request_body_schema["required"]
    assert parser.base_url() == "https://shop.example/v1"


def test_parser_rejects_documents_without_paths(tmp_path):
    bad = tmp_path / "spec.json"
    bad.write_text('{"hello": "world"}', encoding="utf-8")
    with pytest.raises(SpecError):
        ApiTargetParser(str(bad)).load_spec()


def test_schema_validator():
    item = {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}
    assert schema.validate([{"id": 1}], {"type": "array", "items": item}) == []
    errors = schema.validate([{"id": "x"}, {}], {"type": "array", "items": item})
    assert any("$[0].id" in e for e in errors) and any("missing required property 'id'" in e for e in errors)
    assert schema.validate(None, {"type": "string", "nullable": True}) == []


def test_reflection_only_flagged_in_html_responses():
    runner = TestRunner()
    assertion = TestAssertion(assertion_type=AssertionType.NOT_REFLECTED_IN_HTML,
                              expected_value=XSS_PROBE, description="")
    html_ok, _ = runner._evaluate_assertion(assertion, 200, 1, f"<p>{XSS_PROBE}</p>", {"Content-Type": "text/html"})
    json_ok, _ = runner._evaluate_assertion(assertion, 200, 1, f'"{XSS_PROBE}"', {"content-type": "application/json"})
    assert html_ok is False and json_ok is True


def test_reproduction_script_is_valid_python_and_flags_the_bug(api_copy):
    app = load_app(api_copy)
    tests = {t.id: t for t in api_suite(app)}
    test = tests["TC-API-BOUND-NEG-POST-orders"]
    result = TestRunner().run_test(TestClient(app, raise_server_exceptions=False), test)
    script = BugAnalyzer().analyze_failure(test, result).reproduction_code
    compile(script, "reproduce_bug.py", "exec")
    assert "sys.exit(1 if problems else 0)" in script


def test_javascript_forms_are_skipped_and_html_defaults_apply():
    parser = WebTargetParser("https://shop.example")
    parser.raw_html = """
      <a href="/">Home</a><a href="https://shop.example">Home again</a><a href="/#top">Top</a>
      <form id="spa-login"><input name="user"></form>
      <form action="/search"><input name="q"></form>
      <form method="post"><input name="email"></form>
    """
    spec = parser.parse()
    assert [link.href for link in spec.links] == ["https://shop.example/"]
    assert [(f.action, f.method) for f in spec.forms] == [
        ("https://shop.example/search", "GET"),   # no method attribute -> GET
        ("https://shop.example/", "POST"),        # no action -> current page
    ]
