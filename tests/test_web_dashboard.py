"""
End-to-End and Integration tests for Web Dashboard (Stage 7).
Validates dashboard routing, hunt API execution, and approval actions.
"""

from starlette.testclient import TestClient
from web.app import app as web_app
from playground.sample_api import app as sample_api_app
from core.models import TestResult


def test_dashboard_homepage_renders():
    client = TestClient(web_app)
    response = client.get("/")
    assert response.status_code == 200
    assert "BugHunter AI" in response.text
    assert "Human-in-the-Loop" in response.text
    assert "Test Hedefi ve Kriter Belirleme" in response.text


def test_run_hunt_api_endpoint(monkeypatch):
    client = TestClient(web_app)
    openapi_spec = sample_api_app.openapi()

    from core.targets.api_target import ApiTargetParser
    from web.app import runner

    def mock_load(self):
        self.raw_spec = openapi_spec
        return self.raw_spec

    monkeypatch.setattr(ApiTargetParser, "load_spec", mock_load)

    # Mock runner.run_suite to return quick test results for the tests
    def mock_run_suite(tests):
        results = []
        for t in tests:
            passed = "NEG" not in t.id
            results.append(
                TestResult(
                    test_id=t.id,
                    passed=passed,
                    status_code=201 if not passed else 200,
                    elapsed_ms=45.0,
                    failed_assertions=[] if passed else ["Improper status code"]
                )
            )
        return results

    monkeypatch.setattr(runner, "run_suite", mock_run_suite)

    response = client.post(
        "/api/run-hunt",
        json={
            "target_url": "http://127.0.0.1:8001/openapi.json",
            "target_type": "API",
            "criteria": ["FUNCTIONAL", "BOUNDARY", "SECURITY"],
            "sla_threshold_ms": 500.0
        }
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["summary"]["total_tests"] > 0
    assert "proposals" in data
    assert "results" in data
    assert data["summary"]["failed_tests"] >= 1
