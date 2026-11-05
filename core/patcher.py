"""
Human-in-the-Loop Code Patcher & Approval Gate Engine.
Generates Unified Diffs, strictly enforces human approval before touching code,
and supports automated backups and rollbacks.
"""

import difflib
import os
import shutil
from typing import Dict, List, Optional
from core.models import BugEvidence, FixProposal


class HumanApprovalGate:
    """Enforces strict Human-in-the-Loop review before any code modifications."""

    def __init__(self):
        self._proposals: Dict[str, FixProposal] = {}
        self._backups: Dict[str, str] = {}  # target_file -> backup_path

    def register_proposal(self, proposal: FixProposal) -> None:
        self._proposals[proposal.fix_id] = proposal

    def get_proposal(self, fix_id: str) -> Optional[FixProposal]:
        return self._proposals.get(fix_id)

    def list_proposals(self) -> List[FixProposal]:
        return list(self._proposals.values())

    def approve_fix(self, fix_id: str) -> FixProposal:
        """User explicitly grants approval to apply the fix."""
        if fix_id not in self._proposals:
            raise KeyError(f"Fix proposal '{fix_id}' not found.")
        self._proposals[fix_id].approved = True
        return self._proposals[fix_id]

    def reject_fix(self, fix_id: str) -> FixProposal:
        """User explicitly rejects the fix proposal."""
        if fix_id not in self._proposals:
            raise KeyError(f"Fix proposal '{fix_id}' not found.")
        self._proposals[fix_id].approved = False
        return self._proposals[fix_id]

    def apply_patch(self, fix_id: str, create_backup: bool = True) -> bool:
        """
        Applies the approved fix to the target file.
        STRICT HUMAN APPROVAL GATE:
        Raises PermissionError if the proposal has not been explicitly approved.
        """
        proposal = self.get_proposal(fix_id)
        if not proposal:
            raise KeyError(f"Fix proposal '{fix_id}' does not exist.")

        if not proposal.approved:
            raise PermissionError(
                f"🚨 HUMAN APPROVAL GATE BLOCKED: Fix '{fix_id}' has NOT been approved! "
                f"Agent is strictly prohibited from modifying source code without human confirmation."
            )

        if not os.path.exists(proposal.target_file):
            raise FileNotFoundError(f"Target source file '{proposal.target_file}' not found.")

        # Create backup if requested
        if create_backup:
            backup_path = f"{proposal.target_file}.bughunter.bak"
            shutil.copy2(proposal.target_file, backup_path)
            self._backups[proposal.target_file] = backup_path

        # Read original content
        with open(proposal.target_file, "r", encoding="utf-8") as f:
            original_lines = f.readlines()

        # Apply unified diff patch
        patched_lines = self._apply_unified_diff(original_lines, proposal.unified_diff)
        with open(proposal.target_file, "w", encoding="utf-8") as f:
            f.writelines(patched_lines)

        return True

    def rollback(self, target_file: str) -> bool:
        """Restores file from its backup snapshot."""
        backup_path = self._backups.get(target_file) or f"{target_file}.bughunter.bak"
        if os.path.exists(backup_path):
            shutil.copy2(backup_path, target_file)
            return True
        return False

    def _apply_unified_diff(self, original_lines: List[str], diff_text: str) -> List[str]:
        """Applies a clean unified diff patch to original text lines."""
        diff_lines = diff_text.strip().splitlines(keepends=True)
        # Parse diff chunks
        patched = []
        i = 0
        orig_idx = 0

        # Fast and robust patch application for unified chunks
        patch_records = []
        target_lines_to_replace = []
        replacement_lines = []

        collecting = False
        for line in diff_lines:
            if line.startswith("@@"):
                collecting = True
                continue
            if not collecting:
                continue
            if line.startswith("-") and not line.startswith("---"):
                target_lines_to_replace.append(line[1:])
            elif line.startswith("+") and not line.startswith("+++"):
                replacement_lines.append(line[1:])
            elif line.startswith(" "):
                # Context line: if we had pending replacement, flush
                if target_lines_to_replace or replacement_lines:
                    patch_records.append((list(target_lines_to_replace), list(replacement_lines)))
                    target_lines_to_replace.clear()
                    replacement_lines.clear()

        if target_lines_to_replace or replacement_lines:
            patch_records.append((list(target_lines_to_replace), list(replacement_lines)))

        # Apply blocks to original content
        full_text = "".join(original_lines)
        for old_block, new_block in patch_records:
            old_str = "".join(old_block)
            new_str = "".join(new_block)
            if old_str in full_text:
                full_text = full_text.replace(old_str, new_str, 1)

        return [line if line.endswith("\n") else line + "\n" for line in full_text.splitlines()]


class CodePatcher:
    """Synthesizes targeted code fixes and creates Unified Diffs."""

    def __init__(self, approval_gate: Optional[HumanApprovalGate] = None):
        self.gate = approval_gate or HumanApprovalGate()

    def generate_fix(self, evidence: BugEvidence, target_file: str) -> FixProposal:
        """Analyzes bug evidence and target source file to generate a FixProposal."""
        if not os.path.exists(target_file):
            raise FileNotFoundError(f"Target file {target_file} not found.")

        with open(target_file, "r", encoding="utf-8") as f:
            original_code = f.read()

        fixed_code, explanation = self._synthesize_patch(evidence, original_code)
        unified_diff = self._create_unified_diff(
            original_code=original_code,
            fixed_code=fixed_code,
            filepath=target_file
        )

        proposal = FixProposal(
            fix_id=f"FIX-{evidence.bug_id}",
            bug_id=evidence.bug_id,
            target_file=target_file,
            title=f"Proposed Fix for {evidence.bug_id}",
            explanation=explanation,
            unified_diff=unified_diff,
            approved=False  # STRICT DEFAULT: Not approved until human approves
        )

        self.gate.register_proposal(proposal)
        return proposal

    def _synthesize_patch(self, evidence: BugEvidence, code: str) -> (str, str):
        """Generates targeted source fix based on the defect pattern."""
        # 1. Boundary: Negative quantity order bug
        if "BOUND-NEG" in evidence.bug_id or "quantity" in evidence.expected_behavior.lower():
            old_snippet = """    total_price = product["price"] * order.quantity"""
            new_snippet = """    if order.quantity <= 0:
        raise HTTPException(status_code=422, detail="Quantity must be greater than zero")
    total_price = product["price"] * order.quantity"""
            if old_snippet in code:
                return code.replace(old_snippet, new_snippet, 1), (
                    "Added validation check ensuring order.quantity > 0, rejecting negative "
                    "or zero quantities with HTTP 422 Unprocessable Entity."
                )

        # 2. Server Crash: Unhandled KeyError on User profile lookup
        if "CRASH-PROFILE" in evidence.bug_id or "profile" in evidence.target_url:
            old_snippet = """    profile_data = USER_PROFILES_DB[user_id]"""
            new_snippet = """    if user_id not in USER_PROFILES_DB:
        raise HTTPException(status_code=404, detail="User profile not found")
    profile_data = USER_PROFILES_DB[user_id]"""
            if old_snippet in code:
                return code.replace(old_snippet, new_snippet, 1), (
                    "Replaced direct unchecked dictionary access with existence verification; "
                    "returns clean HTTP 404 Not Found instead of crashing with 500 KeyError."
                )

        # 3. Security: Missing auth on DELETE product
        if "SEC-NOAUTH" in evidence.bug_id or (evidence.method == "DELETE" and "product" in evidence.target_url):
            old_snippet = """    deleted = PRODUCTS_DB.pop(product_id)"""
            new_snippet = """    if not authorization or authorization != "Bearer admin-token":
        raise HTTPException(status_code=401, detail="Unauthorized: Admin bearer token required")
    deleted = PRODUCTS_DB.pop(product_id)"""
            if old_snippet in code:
                return code.replace(old_snippet, new_snippet, 1), (
                    "Enforced mandatory Authorization check requiring valid admin bearer token "
                    "before allowing destructive DELETE operations, returning 401 Unauthorized."
                )

        # 4. Web Form Crash: Missing '@' in email
        if "FORM" in evidence.bug_id or "submit-contact" in evidence.target_url:
            old_snippet = """    domain = email.split("@")[1]"""
            new_snippet = """    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="Valid email address with '@' symbol is required")
    domain = email.split("@")[1]"""
            if old_snippet in code:
                return code.replace(old_snippet, new_snippet, 1), (
                    "Added validation to verify presence of '@' in email string, returning HTTP 422 "
                    "validation error instead of crashing the server with 500 IndexError."
                )

        return code, "No automatic patch pattern matched for this defect."

    def _create_unified_diff(self, original_code: str, fixed_code: str, filepath: str) -> str:
        """Generates standard unified diff output."""
        orig_lines = original_code.splitlines(keepends=True)
        fixed_lines = fixed_code.splitlines(keepends=True)
        diff = difflib.unified_diff(
            orig_lines,
            fixed_lines,
            fromfile=f"a/{os.path.basename(filepath)}",
            tofile=f"b/{os.path.basename(filepath)}",
            lineterm=""
        )
        return "\n".join(diff)
