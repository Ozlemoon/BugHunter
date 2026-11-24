"""
Run comparison (regression view): what changed since the previous test of the same system.

Bugs are matched by the id of the test that found them. Test ids are built from the
endpoint / page and the kind of check, so the same check gets the same id in every run.

- new         : found now, not in the previous run   → something broke
- fixed       : found before, and the same test passes now
- persisting  : found in both runs
- not_retested: found before, but this run did not run that test (other criteria,
                fewer pages ...). Not counted as fixed: nobody checked it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from core.engine import Finding, HuntRun

SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]


def _bugs(run: HuntRun) -> Dict[str, Finding]:
    """Real bugs of a run by test id; probable false alarms are left out (as in the digest)."""
    bugs: Dict[str, Finding] = {}
    for finding in run.findings:
        if finding.remediation is not None and not finding.remediation.noteworthy:
            continue
        key = finding.evidence.test_id or finding.evidence.bug_id
        bugs.setdefault(key, finding)
    return bugs


def _item(key: str, finding: Finding, before: Optional[Finding] = None) -> dict:
    remediation = finding.remediation
    item = {
        "test_id": key,
        "title": remediation.headline if remediation else finding.evidence.title,
        "location": (remediation.location if remediation else "") or finding.evidence.endpoint or "",
        "severity": finding.evidence.severity.value,
        "criterion": finding.evidence.criterion.value,
    }
    if before is not None and before.evidence.severity != finding.evidence.severity:
        item["previous_severity"] = before.evidence.severity.value
    return item


def _ordered(items: List[dict]) -> List[dict]:
    return sorted(items, key=lambda i: (SEVERITY_ORDER.index(i["severity"]) if i["severity"] in SEVERITY_ORDER else 9,
                                        i["title"]))


def compare(current: HuntRun, previous: Optional[HuntRun]) -> dict:
    """The difference between two runs; `previous=None` → nothing to compare with yet."""
    if previous is None:
        return {"previous": None, "new": [], "fixed": [], "persisting": [], "not_retested": [],
                "counts": {"new": 0, "fixed": 0, "persisting": 0, "not_retested": 0},
                "verdict": "first"}
    now, before = _bugs(current), _bugs(previous)
    ran_now = {r.test_id for r in current.results}

    new = [_item(k, f) for k, f in now.items() if k not in before]
    persisting = [_item(k, f, before[k]) for k, f in now.items() if k in before]
    fixed = [_item(k, f) for k, f in before.items() if k not in now and k in ran_now]
    not_retested = [_item(k, f) for k, f in before.items() if k not in now and k not in ran_now]

    counts = {"new": len(new), "fixed": len(fixed), "persisting": len(persisting), "not_retested": len(not_retested)}
    if new:
        verdict = "worse" if not fixed or len(new) > len(fixed) else "mixed"
    elif fixed:
        verdict = "better"
    else:
        verdict = "same"
    return {
        "previous": {"run_id": previous.run_id, "started_at": previous.started_at.isoformat(),
                     "total_tests": previous.summary.total_tests, "failed_tests": previous.summary.failed_tests},
        "current": {"run_id": current.run_id, "started_at": current.started_at.isoformat(),
                    "total_tests": current.summary.total_tests, "failed_tests": current.summary.failed_tests},
        "new": _ordered(new), "fixed": _ordered(fixed), "persisting": _ordered(persisting),
        "not_retested": _ordered(not_retested), "counts": counts, "verdict": verdict,
        "new_serious": sum(1 for i in new if i["severity"] in ("CRITICAL", "HIGH")),
    }


VERDICT_TEXT = {
    "first": "Bu sistemin ilk testi; karşılaştırılacak önceki test yok.",
    "worse": "Durum kötüleşti: yeni hatalar çıktı.",
    "mixed": "Karışık: bazı hatalar düzeldi, ama yenileri de çıktı.",
    "better": "Durum iyileşti: yeni hata yok, bazı hatalar düzeldi.",
    "same": "Değişiklik yok: yeni hata da düzelen hata da yok.",
}


def headline(comparison: dict) -> str:
    """One Turkish sentence, e.g. for an e-mail: '+3 yeni, 5 düzelen, 12 devam eden'."""
    if comparison["previous"] is None:
        return VERDICT_TEXT["first"]
    c = comparison["counts"]
    return f"Önceki teste göre: {c['new']} yeni, {c['fixed']} düzelen, {c['persisting']} devam eden hata."
