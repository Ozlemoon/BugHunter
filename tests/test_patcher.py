"""
Unit tests for Stage 5: CodePatcher & HumanApprovalGate.
Validates Unified Diff generation, strict approval gate enforcement,
safe patch application, and rollback.
"""

import os
import tempfile
import pytest
from core.models import BugEvidence, TargetType, TestCriterion
from core.patcher import CodePatcher, HumanApprovalGate


@pytest.fixture
def sample_code_file():
    code_content = """from fastapi import FastAPI, HTTPException
app = FastAPI()

PRODUCTS_DB = {1: {"id": 1, "price": 10.0, "stock": 50}}
ORDERS_DB = []

@app.post("/orders")
def create_order(order):
    product = PRODUCTS_DB[order.product_id]
    total_price = product["price"] * order.quantity
    product["stock"] -= order.quantity
    return {"order_id": 1}
"""
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".py", encoding="utf-8") as f:
        f.write(code_content)
        path = f.name
    yield path
    if os.path.exists(path):
        os.remove(path)
    bak_path = f"{path}.bughunter.bak"
    if os.path.exists(bak_path):
        os.remove(bak_path)


def test_human_approval_gate_blocks_unapproved_fix(sample_code_file):
    evidence = BugEvidence(
        bug_id="BUG-API-BOUND-NEG-ORDER",
        title="Defect: Negative quantity order",
        target_type=TargetType.API,
        criterion=TestCriterion.BOUNDARY,
        target_url="http://testserver/orders",
        method="POST",
        expected_behavior="Should reject negative quantities with 422",
        actual_behavior="Accepted with 201",
        reproduction_code="print('reproduce')",
        root_cause_hypothesis="Missing quantity <= 0 validation",
        severity="HIGH"
    )

    gate = HumanApprovalGate()
    patcher = CodePatcher(approval_gate=gate)

    proposal = patcher.generate_fix(evidence, target_file=sample_code_file)

    assert proposal.approved is False
    assert "--- a/" in proposal.unified_diff
    assert "+++ b/" in proposal.unified_diff
    assert "+    if order.quantity <= 0:" in proposal.unified_diff

    # STRICT GATE VERIFICATION: Attempting to apply without approval MUST raise PermissionError
    with pytest.raises(PermissionError) as exc_info:
        gate.apply_patch(proposal.fix_id)
    assert "HUMAN APPROVAL GATE BLOCKED" in str(exc_info.value)


def test_human_approval_gate_allows_approved_fix_and_rollback(sample_code_file):
    evidence = BugEvidence(
        bug_id="BUG-API-BOUND-NEG-ORDER",
        title="Defect: Negative quantity order",
        target_type=TargetType.API,
        criterion=TestCriterion.BOUNDARY,
        target_url="http://testserver/orders",
        method="POST",
        expected_behavior="Should reject negative quantities with 422",
        actual_behavior="Accepted with 201",
        reproduction_code="print('reproduce')",
        root_cause_hypothesis="Missing quantity <= 0 validation",
        severity="HIGH"
    )

    gate = HumanApprovalGate()
    patcher = CodePatcher(approval_gate=gate)
    proposal = patcher.generate_fix(evidence, target_file=sample_code_file)

    # Human explicitly grants approval
    gate.approve_fix(proposal.fix_id)
    assert proposal.approved is True

    # Now applying patch succeeds
    applied = gate.apply_patch(proposal.fix_id)
    assert applied is True

    # Verify target file has the patched validation code
    with open(sample_code_file, "r", encoding="utf-8") as f:
        patched_content = f.read()
    assert "if order.quantity <= 0:" in patched_content
    assert "raise HTTPException(status_code=422" in patched_content

    # Verify backup was created
    bak_path = f"{sample_code_file}.bughunter.bak"
    assert os.path.exists(bak_path)

    # Verify rollback restores the original content
    gate.rollback(sample_code_file)
    with open(sample_code_file, "r", encoding="utf-8") as f:
        restored_content = f.read()
    assert "if order.quantity <= 0:" not in restored_content
