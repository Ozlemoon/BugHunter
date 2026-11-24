"""
Report exporters for a HuntRun.

- JSON  : full machine-readable record (same shape as the run history file)
- JUnit : XML understood by GitHub Actions, GitLab, Jenkins, Azure DevOps
- HTML  : self-contained page (no CDN) to attach to tickets or share offline
"""

from __future__ import annotations

import base64
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

if TYPE_CHECKING:
    from core.engine import HuntRun

TEMPLATES = Path(__file__).resolve().parent / "templates"
_env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]))

def environment() -> Environment:
    """The Jinja environment for core/templates (reports and e-mails)."""
    return _env


FORMATS = {
    "json": ("application/json", "json"),
    "junit": ("application/xml", "xml"),
    "html": ("text/html; charset=utf-8", "html"),
    "pdf": ("application/pdf", "pdf"),
}


class PdfError(RuntimeError):
    """A message that can be shown to the user as is."""


def render_pdf(run: HuntRun, screenshots: Optional[dict] = None, browser_path: str = "",
               comparison: Optional[dict] = None) -> bytes:
    """The HTML report printed to PDF by an installed Edge/Chrome/Chromium (nothing to install)."""
    from core import screenshots as browser_tools

    executable = browser_tools.find_browser(browser_path)
    if executable is None:
        raise PdfError("PDF için Edge, Chrome veya Chromium bulunamadı; HTML rapor kullanılabilir.")
    html = render(run, "html", screenshots, comparison=comparison)
    try:
        with browser_tools.open_browser(executable) as browser:
            return browser.print_pdf(html, footer=f"BugHunter test raporu · {run.run_id}")
    except Exception as error:  # browser could not start, timed out ...
        raise PdfError(f"PDF oluşturulamadı: {error}") from error


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


def to_html(run: HuntRun, screenshots: Optional[dict] = None, comparison: Optional[dict] = None) -> str:
    results = {r.test_id: r for r in run.results}
    rows = [(t, results.get(t.id)) for t in run.tests]
    images = {test_id: "data:image/png;base64," + base64.b64encode(shot["png"]).decode()
              for test_id, shot in (screenshots or {}).items()}  # embedded: the file stays self-contained
    from core.comparison import VERDICT_TEXT

    return _env.get_template("report.html").render(run=run, rows=rows, screenshots=images,
                                                   comparison=comparison, verdict_text=VERDICT_TEXT)


def render(run: HuntRun, fmt: str, screenshots: Optional[dict] = None, comparison: Optional[dict] = None) -> str:
    """`screenshots` ({test_id: {"png": bytes, ...}}) and `comparison` (core.comparison) go into the HTML report."""
    from core.redaction import redact_run

    renderers = {"json": to_json, "junit": to_junit, "html": to_html}
    if fmt == "pdf":
        raise ValueError("PDF is binary: use render_pdf().")
    if fmt not in renderers:
        raise ValueError(f"Unknown report format '{fmt}'. Use one of: {', '.join(renderers)}")
    if fmt == "html":
        return to_html(redact_run(run), screenshots, comparison)
    return renderers[fmt](redact_run(run))


def write(run: HuntRun, fmt: str, path: Path, screenshots: Optional[dict] = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "pdf":
        path.write_bytes(render_pdf(run, screenshots))
    else:
        path.write_text(render(run, fmt, screenshots), encoding="utf-8")
    return path
