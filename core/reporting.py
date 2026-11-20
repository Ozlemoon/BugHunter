"""
Report exporters for a HuntRun.

- JSON  : full machine-readable record (same shape as the run history file)
- JUnit : XML understood by GitHub Actions, GitLab, Jenkins, Azure DevOps
- HTML  : self-contained page (no CDN) to attach to tickets or share offline
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader, select_autoescape

if TYPE_CHECKING:
    from core.engine import HuntRun

TEMPLATES = Path(__file__).resolve().parent / "templates"
_env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]))

FORMATS = {
    "json": ("application/json", "json"),
    "junit": ("application/xml", "xml"),
    "html": ("text/html; charset=utf-8", "html"),
}


def to_json(run: HuntRun) -> str:
    return run.model_dump_json(indent=2)


def to_junit(run: HuntRun) -> str:
    results = {r.test_id: r for r in run.results}
    findings = {f.evidence.test_id: f for f in run.findings}
    root = ET.Element("testsuites", name=f"BugHunter {run.run_id}",
                      tests=str(run.summary.total_tests), failures=str(run.summary.failed_tests),
                      time=f"{run.summary.duration_ms / 1000:.3f}")

    by_criterion: dict = {}
    for test in run.tests:
        by_criterion.setdefault(test.criterion.value, []).append(test)

    for criterion, tests in by_criterion.items():
        failures = sum(1 for t in tests if t.id in results and not results[t.id].passed)
        suite = ET.SubElement(root, "testsuite", name=f"bughunter.{criterion.lower()}",
                              tests=str(len(tests)), failures=str(failures),
                              timestamp=run.started_at.isoformat())
        for test in tests:
            result = results.get(test.id)
            case = ET.SubElement(suite, "testcase", name=f"{test.id}: {test.title}",
                                 classname=f"bughunter.{criterion.lower()}",
                                 time=f"{(result.elapsed_ms if result else 0) / 1000:.3f}")
            if result and not result.passed:
                finding = findings.get(test.id)
                severity = finding.evidence.severity.value if finding else "UNKNOWN"
                failure = ET.SubElement(case, "failure", type=severity,
                                        message="; ".join(result.failed_assertions)[:500])
                lines = [f"{test.method} {test.url}", f"HTTP {result.status_code}  {result.elapsed_ms}ms"]
                if finding:
                    lines += ["", "Root cause: " + finding.evidence.root_cause_hypothesis]
                failure.text = "\n".join(lines)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def to_html(run: HuntRun) -> str:
    results = {r.test_id: r for r in run.results}
    rows = [(t, results.get(t.id)) for t in run.tests]
    return _env.get_template("report.html").render(run=run, rows=rows)


def render(run: HuntRun, fmt: str) -> str:
    from core.redaction import redact_run

    renderers = {"json": to_json, "junit": to_junit, "html": to_html}
    if fmt not in renderers:
        raise ValueError(f"Unknown report format '{fmt}'. Use one of: {', '.join(renderers)}")
    return renderers[fmt](redact_run(run))


def write(run: HuntRun, fmt: str, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(run, fmt), encoding="utf-8")
    return path
