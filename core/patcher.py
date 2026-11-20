"""
Human-in-the-Loop Code Patcher & Approval Gate Engine.

- CodePatcher turns BugEvidence into a FixProposal using an explicit registry of
  fix rules (matched on HTTP method, templated endpoint and failure type).
- HumanApprovalGate refuses to touch source code until a human approves, checks
  that the file has not changed since the diff was produced, applies the diff
  hunk-by-hunk with context verification, and keeps a backup for rollback.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlparse

from core.models import BugEvidence, FixProposal, FixStatus, TestCriterion

BACKUP_SUFFIX = ".bughunter.bak"


class PatchConflictError(RuntimeError):
    """The diff no longer matches the file it is meant to change."""


def file_sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _read_source(path: str) -> tuple[str, str]:
    """Returns (text with \\n newlines, original newline style)."""
    with open(path, "rb") as f:
        raw = f.read()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    return raw.decode("utf-8").replace("\r\n", "\n"), newline


def _write_source(path: str, text: str, newline: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text.replace("\n", newline))


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def apply_unified_diff(original: str, diff_text: str, fuzz: int = 3) -> str:
    """
    Applies a unified diff to `original` (\\n-separated text).

    Every hunk's context and removed lines must match the original, at the
    declared line or within `fuzz` lines of it; otherwise PatchConflictError
    is raised and nothing is changed.
    """
    lines = original.split("\n")
    trailing_newline = original.endswith("\n")
    if trailing_newline:
        lines = lines[:-1]

    hunks: List[tuple[int, List[str], List[str]]] = []
    diff_lines = diff_text.split("\n")
    index = 0
    while index < len(diff_lines):
        header = _HUNK_HEADER.match(diff_lines[index])
        index += 1
        if not header:
            continue
        old_left = int(header.group(2) or 1)
        new_left = int(header.group(4) or 1)
        old_block: List[str] = []
        new_block: List[str] = []
        # Consume exactly the number of lines the header declares.
        while (old_left > 0 or new_left > 0) and index < len(diff_lines):
            raw = diff_lines[index]
            index += 1
            if raw.startswith("\\"):
                continue
            tag, body = (raw[:1], raw[1:]) if raw else (" ", "")
            if tag in (" ", "-"):
                old_block.append(body)
                old_left -= 1
            if tag in (" ", "+"):
                new_block.append(body)
                new_left -= 1
            if tag not in (" ", "-", "+"):
                raise PatchConflictError(f"Malformed diff line: {raw!r}")
        if old_left > 0 or new_left > 0:
            raise PatchConflictError("Diff hunk is truncated.")
        hunks.append((int(header.group(1)), old_block, new_block))

    if not hunks:
        raise PatchConflictError("Diff contains no hunks.")

    offset = 0
    for start, old_block, new_block in hunks:
        expected = max(start - 1, 0) + offset
        position = None
        for delta in sorted(range(-fuzz, fuzz + 1), key=abs):
            at = expected + delta
            if 0 <= at <= len(lines) - len(old_block) and lines[at:at + len(old_block)] == old_block:
                position = at
                break
        if position is None:
            raise PatchConflictError(f"Hunk starting at line {start} does not match the current file.")
        lines[position:position + len(old_block)] = new_block
        offset += len(new_block) - len(old_block)

    return "\n".join(lines) + ("\n" if trailing_newline else "")


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

    def _require(self, fix_id: str) -> FixProposal:
        proposal = self._proposals.get(fix_id)
        if proposal is None:
            raise KeyError(f"Fix proposal '{fix_id}' not found.")
        return proposal

    def approve_fix(self, fix_id: str) -> FixProposal:
        """User explicitly grants approval to apply the fix."""
        proposal = self._require(fix_id)
        proposal.approved = True
        proposal.status = FixStatus.APPROVED
        return proposal

    def reject_fix(self, fix_id: str) -> FixProposal:
        """User explicitly rejects the fix proposal."""
        proposal = self._require(fix_id)
        proposal.approved = False
        proposal.status = FixStatus.REJECTED
        return proposal

    def apply_patch(self, fix_id: str, create_backup: bool = True) -> bool:
        """
        Applies the approved fix to the target file.
        STRICT HUMAN APPROVAL GATE:
        Raises PermissionError if the proposal has not been explicitly approved.
        """
        proposal = self._require(fix_id)

        if not proposal.approved:
            raise PermissionError(
                f"🚨 HUMAN APPROVAL GATE BLOCKED: Fix '{fix_id}' has NOT been approved! "
                f"Agent is strictly prohibited from modifying source code without human confirmation."
            )
        if not proposal.patch_available or not proposal.unified_diff.strip():
            raise PatchConflictError(f"Fix '{fix_id}' has no applicable patch.")
        if proposal.status == FixStatus.APPLIED:
            raise PatchConflictError(f"Fix '{fix_id}' is already applied.")
        if not os.path.exists(proposal.target_file):
            raise FileNotFoundError(f"Target source file '{proposal.target_file}' not found.")
        if proposal.source_sha256 and file_sha256(proposal.target_file) != proposal.source_sha256:
            raise PatchConflictError(
                f"'{os.path.basename(proposal.target_file)}' changed after this fix was proposed. "
                "Re-run the tests to get a fresh proposal."
            )

        original, newline = _read_source(proposal.target_file)
        patched = apply_unified_diff(original, proposal.unified_diff)  # raises before any write

        if create_backup:
            backup_path = f"{proposal.target_file}{BACKUP_SUFFIX}"
            shutil.copy2(proposal.target_file, backup_path)
            self._backups[proposal.target_file] = backup_path

        _write_source(proposal.target_file, patched, newline)
        proposal.status = FixStatus.APPLIED
        return True

    def rollback(self, target_file: str) -> bool:
        """Restores file from its backup snapshot (undoes the most recent patch)."""
        backup_path = self._backups.get(target_file) or f"{target_file}{BACKUP_SUFFIX}"
        if not os.path.exists(backup_path):
            return False
        shutil.copy2(backup_path, target_file)
        for proposal in self._proposals.values():
            if proposal.target_file == target_file and proposal.status == FixStatus.APPLIED:
                proposal.status = FixStatus.ROLLED_BACK
                proposal.approved = False
        return True


# ---------------------------------------------------------------------------
# Fix rule registry
# ---------------------------------------------------------------------------

def _endpoint(evidence: BugEvidence) -> str:
    return evidence.endpoint or urlparse(evidence.target_url).path or "/"


def _is_server_error(evidence: BugEvidence) -> bool:
    if evidence.status_code is not None:
        return 500 <= evidence.status_code <= 599
    return "5xx" in evidence.actual_behavior or "HTTP 5" in evidence.actual_behavior


@dataclass(frozen=True)
class FixRule:
    name: str
    matches: Callable[[BugEvidence], bool]
    anchor: str
    replacement: str
    explanation: str


FIX_RULES: List[FixRule] = [
    FixRule(
        name="order-quantity-must-be-positive",
        matches=lambda e: e.method == "POST" and _endpoint(e) == "/orders"
        and e.criterion == TestCriterion.BOUNDARY,
        anchor='    total_price = product["price"] * order.quantity',
        replacement='    if order.quantity <= 0:\n'
                    '        raise HTTPException(status_code=422, detail="Quantity must be greater than zero")\n'
                    '    total_price = product["price"] * order.quantity',
        explanation="Added validation check ensuring order.quantity > 0, rejecting negative "
                    "or zero quantities with HTTP 422 Unprocessable Entity.",
    ),
    FixRule(
        name="product-values-must-be-non-negative",
        matches=lambda e: e.method == "POST" and _endpoint(e) == "/products"
        and e.criterion == TestCriterion.BOUNDARY,
        anchor="    new_id = max(PRODUCTS_DB.keys(), default=0) + 1",
        replacement="    if product.price < 0 or product.stock < 0:\n"
                    '        raise HTTPException(status_code=422, detail="Price and stock must not be negative")\n'
                    "    new_id = max(PRODUCTS_DB.keys(), default=0) + 1",
        explanation="Rejects negative price or stock with HTTP 422 before the product is stored.",
    ),
    FixRule(
        name="missing-profile-returns-404",
        matches=lambda e: e.method == "GET" and _endpoint(e).endswith("/profile") and _is_server_error(e),
        anchor="    profile_data = USER_PROFILES_DB[user_id]",
        replacement="    if user_id not in USER_PROFILES_DB:\n"
                    '        raise HTTPException(status_code=404, detail="User profile not found")\n'
                    "    profile_data = USER_PROFILES_DB[user_id]",
        explanation="Replaced direct unchecked dictionary access with existence verification; "
                    "returns clean HTTP 404 Not Found instead of crashing with 500 KeyError.",
    ),
    FixRule(
        name="delete-product-requires-admin",
        matches=lambda e: e.method == "DELETE" and _endpoint(e).startswith("/products/")
        and e.criterion == TestCriterion.SECURITY,
        anchor="    deleted = PRODUCTS_DB.pop(product_id)",
        replacement='    if authorization != "Bearer admin-token":\n'
                    '        raise HTTPException(status_code=401, detail="Unauthorized: Admin bearer token required")\n'
                    "    deleted = PRODUCTS_DB.pop(product_id)",
        explanation="Enforced mandatory Authorization check requiring valid admin bearer token "
                    "before allowing destructive DELETE operations, returning 401 Unauthorized.",
    ),
    FixRule(
        name="contact-email-must-contain-at",
        matches=lambda e: e.method == "POST" and _endpoint(e) == "/submit-contact" and _is_server_error(e),
        anchor='    domain = email.split("@")[1]',
        replacement='    if not email or "@" not in email:\n'
                    "        raise HTTPException(status_code=422, detail=\"Valid email address with '@' symbol is required\")\n"
                    '    domain = email.split("@")[1]',
        explanation="Added validation to verify presence of '@' in email string, returning HTTP 422 "
                    "validation error instead of crashing the server with 500 IndexError.",
    ),
    FixRule(
        name="escape-search-query",
        matches=lambda e: "xss" in e.tags and _endpoint(e) == "/search",
        anchor='    heading = f"<h1>Results for {q}</h1>"',
        replacement='    heading = f"<h1>Results for {html.escape(q)}</h1>"',
        explanation="HTML-escapes the search query before rendering it, closing the reflected XSS hole.",
    ),
    FixRule(
        name="add-security-headers-middleware",
        matches=lambda e: "security-headers" in e.tags,
        anchor="templates = Jinja2Templates(directory=str(TEMPLATES_DIR))",
        replacement="templates = Jinja2Templates(directory=str(TEMPLATES_DIR))\n"
                    "\n"
                    "\n"
                    '@app.middleware("http")\n'
                    "async def security_headers(request: Request, call_next):\n"
                    "    response = await call_next(request)\n"
                    '    response.headers.setdefault("X-Content-Type-Options", "nosniff")\n'
                    '    response.headers.setdefault("X-Frame-Options", "DENY")\n'
                    '    response.headers.setdefault("Content-Security-Policy", '
                    "\"default-src 'self'; style-src 'self' 'unsafe-inline'\")\n"
                    "    return response",
        explanation="Adds a middleware that sends X-Content-Type-Options, X-Frame-Options and a "
                    "Content-Security-Policy on every response.",
    ),
]


class CodePatcher:
    """Synthesizes targeted code fixes and creates Unified Diffs."""

    def __init__(self, approval_gate: Optional[HumanApprovalGate] = None, rules: Optional[List[FixRule]] = None):
        self.gate = approval_gate or HumanApprovalGate()
        self.rules = rules if rules is not None else FIX_RULES

    def generate_fix(self, evidence: BugEvidence, target_file: str) -> FixProposal:
        """Analyzes bug evidence and target source file to generate a FixProposal."""
        if not os.path.exists(target_file):
            raise FileNotFoundError(f"Target file {target_file} not found.")

        original_code, _ = _read_source(target_file)
        fixed_code, explanation, rule_name = self._synthesize_patch(evidence, original_code)
        unified_diff = self._create_unified_diff(original_code, fixed_code, target_file)

        proposal = FixProposal(
            fix_id=f"FIX-{evidence.bug_id}",
            bug_id=evidence.bug_id,
            target_file=target_file,
            title=f"Proposed Fix for {evidence.bug_id}",
            explanation=explanation,
            unified_diff=unified_diff,
            approved=False,  # STRICT DEFAULT: Not approved until human approves
            patch_available=bool(unified_diff),
            rule=rule_name,
            source_sha256=file_sha256(target_file),
        )
        self.gate.register_proposal(proposal)
        return proposal

    def _synthesize_patch(self, evidence: BugEvidence, code: str) -> tuple[str, str, Optional[str]]:
        """Returns (fixed_code, explanation, rule_name) for the first rule that applies."""
        for rule in self.rules:
            if rule.matches(evidence) and code.count(rule.anchor) == 1:
                return code.replace(rule.anchor, rule.replacement, 1), rule.explanation, rule.name
        return code, "No safe automatic fix is known for this defect; review the evidence manually.", None

    def _create_unified_diff(self, original_code: str, fixed_code: str, filepath: str) -> str:
        """Generates standard unified diff output (empty string when nothing changes)."""
        name = os.path.basename(filepath)
        diff = difflib.unified_diff(
            original_code.splitlines(),
            fixed_code.splitlines(),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
            lineterm="",
        )
        text = "\n".join(diff)
        return f"{text}\n" if text else ""
