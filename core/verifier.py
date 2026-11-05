"""
Regression Verification & Comparative Reporting Engine.
Validates whether a fix successfully resolves the target defect and verifies
that no regressions were introduced to the existing test suite.
"""

from typing import Dict, List, Optional
from core.models import FixProposal, RegressionReport, TestCase, TestResult
from core.patcher import HumanApprovalGate
from core.runner import TestRunner


class RegressionVerifier:
    """Re-executes test suites and verifies defect resolution and lack of regressions."""
    __test__ = False

    def verify(
        self,
        runner: TestRunner,
        client,
        all_tests: List[TestCase],
        target_test: TestCase,
        before_results: List[TestResult],
        proposal: FixProposal,
        gate: Optional[HumanApprovalGate] = None,
        auto_rollback_on_regression: bool = False
    ) -> RegressionReport:
        """
        Executes regression verification cycle:
        1. Re-runs the specific failed test to ensure fix resolved the bug.
        2. Re-runs all other tests to ensure no regressions occurred.
        3. Computes Before vs After metrics and generates a verdict.
        """
        # Map before results: test_id -> passed
        before_map: Dict[str, bool] = {r.test_id: r.passed for r in before_results}
        before_pass = sum(1 for p in before_map.values() if p)
        before_fail = sum(1 for p in before_map.values() if not p)

        # 1. Re-run target test
        target_res = runner.run_test(client, target_test)
        bug_fixed = target_res.passed

        # 2. Re-run full suite
        after_results: List[TestResult] = []
        regressions: List[str] = []

        for test in all_tests:
            res = runner.run_test(client, test)
            after_results.append(res)

            # Check for regression: was passing before, now failing!
            was_passing = before_map.get(test.id, False)
            if was_passing and not res.passed:
                failure_detail = f"{test.id} ({test.title}): {'; '.join(res.failed_assertions)}"
                regressions.append(failure_detail)

        after_pass = sum(1 for r in after_results if r.passed)
        after_fail = sum(1 for r in after_results if not r.passed)

        # 3. Determine verdict
        if not bug_fixed:
            verdict = "FIX_UNSUCCESSFUL"
            summary = (
                f"❌ The applied patch did not resolve the defect in {target_test.id}. "
                f"Target test is still failing."
            )
        elif regressions:
            verdict = "REGRESSION_DETECTED"
            summary = (
                f"⚠️ Target bug was fixed, but {len(regressions)} regression(s) were introduced "
                f"in other endpoints!"
            )
            if auto_rollback_on_regression and gate:
                gate.rollback(proposal.target_file)
                summary += " [Auto-Rollback Triggered]"
        else:
            verdict = "CLEAN_FIX"
            summary = (
                f"✅ Target defect {proposal.bug_id} is completely resolved with ZERO regressions. "
                f"Passing tests increased from {before_pass} to {after_pass}."
            )

        return RegressionReport(
            fix_id=proposal.fix_id,
            bug_id=proposal.bug_id,
            bug_fixed=bug_fixed,
            before_pass_count=before_pass,
            before_fail_count=before_fail,
            after_pass_count=after_pass,
            after_fail_count=after_fail,
            regressions_detected=regressions,
            verdict=verdict,
            summary=summary
        )
