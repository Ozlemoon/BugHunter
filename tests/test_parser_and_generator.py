"""
Unit tests for Stage 3: Parser and Generator modules.
"""

from core.models import TestCriterion, TargetType
from core.targets.api_target import ApiTargetParser
from core.targets.web_target import WebTargetParser
from core.generator import TestGenerator
from playground.sample_api import app as sample_api_app


def test_api_parser_and_generator():
    openapi_spec = sample_api_app.openapi()
    parser = ApiTargetParser(spec_source="")
    parser.raw_spec = openapi_spec
    endpoints = parser.parse()

    assert len(endpoints) > 0
    # Ensure endpoints like /products, /orders, /users/{user_id}/profile are found
    paths = [ep.path for ep in endpoints]
    assert "/products" in paths
    assert "/orders" in paths
    assert "/users/{user_id}/profile" in paths

    # Generator test
    generator = TestGenerator(base_url="http://127.0.0.1:8001")
    tests = generator.generate_api_tests(endpoints)

    assert len(tests) > 0
    test_criteria = {t.criterion for t in tests}
    assert TestCriterion.FUNCTIONAL in test_criteria
    assert TestCriterion.BOUNDARY in test_criteria
    assert TestCriterion.SECURITY in test_criteria
    assert TestCriterion.PERFORMANCE in test_criteria


def test_web_parser_and_generator():
    html_sample = """
    <html>
      <head><title>Test Shop</title></head>
      <body>
        <a href="/about">About Us</a>
        <a href="/broken-link">Broken</a>
        <img src="/static/logo.png" alt="Logo">
        <form action="/submit" method="POST">
          <input type="text" name="username" required>
          <input type="email" name="email">
        </form>
      </body>
    </html>
    """
    web_parser = WebTargetParser(target_url="http://127.0.0.1:8002")
    web_parser.raw_html = html_sample
    page_spec = web_parser.parse()

    assert page_spec.title == "Test Shop"
    assert len(page_spec.links) == 2
    assert len(page_spec.assets) == 1
    assert len(page_spec.forms) == 1
    assert len(page_spec.forms[0].fields) == 2

    # Web Generator test
    generator = TestGenerator(base_url="http://127.0.0.1:8002")
    web_tests = generator.generate_web_tests(page_spec)

    assert len(web_tests) >= 3  # Links + Asset + Form validation
    web_tags = [tag for t in web_tests for tag in t.tags]
    assert "broken-link-crawler" in web_tags
    assert "forms" in web_tags
