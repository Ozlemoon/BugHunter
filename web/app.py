"""
BugHunter AI Interactive Web Dashboard.
FastAPI backend providing REST API and UI for autonomous test triggering,
evidence exploration, human approval gate, and regression verification.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from core.analyzer import BugAnalyzer
from core.generator import TestGenerator
from core.models import (
    FixProposal,
    TargetType,
    TestCase,
    TestCriterion,
    TestResult,
)
from core.patcher import CodePatcher, HumanApprovalGate
from core.runner import TestRunner
from core.targets.api_target import ApiTargetParser
from core.targets.web_target import WebTargetParser
from core.verifier import RegressionVerifier

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

app = FastAPI(
    title="BugHunter AI Web Dashboard",
    version="1.0.0",
    description="Interactive Web Control Panel for Autonomous Software Testing & Bug Analysis"
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Global in-memory state for active session
approval_gate = HumanApprovalGate()
patcher = CodePatcher(approval_gate=approval_gate)
verifier = RegressionVerifier()
runner = TestRunner()
analyzer = BugAnalyzer()

SESSION_STATE: Dict[str, Any] = {
    "all_tests": [],
    "before_results": [],
    "test_map": {},          # test_id -> TestCase
    "proposals": {},         # fix_id -> dict(proposal, evidence, test)
}


class HuntRequest(BaseModel):
    target_url: str
    target_type: str = "API"
    criteria: List[str] = ["FUNCTIONAL", "BOUNDARY", "SECURITY", "PERFORMANCE", "WEB_VALIDATION"]
    sla_threshold_ms: float = 500.0


class ApproveRequest(BaseModel):
    fix_id: str


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Renders the main BugHunter interactive dashboard."""
    return templates.TemplateResponse(request=request, name="dashboard.html")


@app.post("/api/run-hunt")
async def run_hunt(payload: HuntRequest):
    """
    Executes the autonomous test cycle:
    1. Parse Documentation / Web DOM
    2. Generate Criteria-Driven Test Scenarios
    3. Execute Tests & Measure Metrics
    4. Isolate Deficiencies & Gather Evidence
    5. Formulate Fix Proposals & Unified Diffs (Approved = False)
    """
    target_type = TargetType[payload.target_type]
    criteria_set: Set[TestCriterion] = {
        TestCriterion[c] for c in payload.criteria if c in TestCriterion.__members__
    }

    # 1. Parse Target
    tests: List[TestCase] = []
    if target_type == TargetType.API:
        parser = ApiTargetParser(payload.target_url)
        endpoints = parser.parse()
        base_url = payload.target_url.rsplit("/", 1)[0]
        generator = TestGenerator(base_url=base_url, criteria=criteria_set, sla_threshold_ms=payload.sla_threshold_ms)
        tests = generator.generate_api_tests(endpoints)
    else:
        web_parser = WebTargetParser(payload.target_url)
        page_spec = web_parser.parse()
        generator = TestGenerator(base_url=payload.target_url, criteria=criteria_set, sla_threshold_ms=payload.sla_threshold_ms)
        tests = generator.generate_web_tests(page_spec)

    # 2. Run Test Suite
    results = runner.run_suite(tests)

    # 3. Analyze Failures and Synthesize Fix Proposals
    proposals_payload = []
    SESSION_STATE["all_tests"] = tests
    SESSION_STATE["before_results"] = results
    SESSION_STATE["test_map"] = {t.id: t for t in tests}
    SESSION_STATE["proposals"].clear()

    # Determine default target source file for demo playground if running locally
    target_file = "playground/sample_api.py" if target_type == TargetType.API else "playground/sample_web.py"
    target_file_abs = os.path.abspath(target_file)

    for test in tests:
        res = next((r for r in results if r.test_id == test.id), None)
        if res and not res.passed:
            evidence = analyzer.analyze_failure(test, res)
            proposal = patcher.generate_fix(evidence, target_file=target_file_abs)

            SESSION_STATE["proposals"][proposal.fix_id] = {
                "proposal": proposal,
                "evidence": evidence,
                "test": test
            }

            proposals_payload.append({
                "proposal": proposal.model_dump(),
                "evidence": evidence.model_dump()
            })

    passed_count = sum(1 for r in results if r.passed)
    failed_count = sum(1 for r in results if not r.passed)

    return {
        "status": "success",
        "summary": {
            "total_tests": len(tests),
            "passed_tests": passed_count,
            "failed_tests": failed_count,
            "target_type": target_type.value,
        },
        "proposals": proposals_payload,
        "results": [
            {
                "test_id": r.test_id,
                "passed": r.passed,
                "status_code": r.status_code,
                "elapsed_ms": r.elapsed_ms,
                "method": SESSION_STATE["test_map"][r.test_id].method,
                "url": SESSION_STATE["test_map"][r.test_id].url,
                "criterion": SESSION_STATE["test_map"][r.test_id].criterion.value,
                "failed_assertions": r.failed_assertions
            }
            for r in results
        ]
    }


@app.post("/api/approve-fix")
async def approve_fix(payload: ApproveRequest):
    """
    Human Approves Fix:
    1. HumanApprovalGate approves and applies patch to source code.
    2. RegressionVerifier re-runs tests to verify resolution and check for regressions.
    """
    fix_id = payload.fix_id
    if fix_id not in SESSION_STATE["proposals"]:
        raise HTTPException(status_code=404, detail="Fix proposal not found.")

    entry = SESSION_STATE["proposals"][fix_id]
    proposal: FixProposal = entry["proposal"]
    target_test: TestCase = entry["test"]

    # Strict Human Approval Gate
    approval_gate.approve_fix(fix_id)
    approval_gate.apply_patch(fix_id, create_backup=True)

    # Re-run Regression Verification
    report = verifier.verify(
        runner=runner,
        client=None, # Will use httpx internally inside run_suite/run_test
        all_tests=SESSION_STATE["all_tests"],
        target_test=target_test,
        before_results=SESSION_STATE["before_results"],
        proposal=proposal,
        gate=approval_gate,
        auto_rollback_on_regression=True
    )

    return {
        "status": "success",
        "fix_id": fix_id,
        "report": report.model_dump()
    }


@app.post("/api/reject-fix")
async def reject_fix(payload: ApproveRequest):
    """Human rejects a proposed patch."""
    fix_id = payload.fix_id
    if fix_id in SESSION_STATE["proposals"]:
        approval_gate.reject_fix(fix_id)
    return {"status": "rejected", "fix_id": fix_id}


@app.post("/api/rollback-fix")
async def rollback_fix(payload: Dict[str, str]):
    """Rollback modified file to backup snapshot."""
    file_path = payload.get("file_path")
    if file_path and os.path.exists(file_path):
        approval_gate.rollback(file_path)
        return {"status": "success", "message": f"Rolled back {file_path}"}
    raise HTTPException(status_code=400, detail="Invalid file path")
