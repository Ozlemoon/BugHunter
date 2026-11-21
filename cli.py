"""
BugHunter AI — terminal interface.

- `hunt`  : interactive run with diff review and a human approval prompt per fix
- `scan`  : headless run for CI pipelines (reports + exit codes, never patches)
- `runs`  : list the run history
- `report`: export a stored run as JSON / JUnit / HTML
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import httpx
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn
from rich.prompt import Confirm
from rich.syntax import Syntax
from rich.table import Table

from core import reporting
from core.config import get_settings
from core.engine import HuntConfig, HuntRun, HuntSession, describe_connection_error, playground_source_for
from core.generator import ALL_CRITERIA
from core.models import Severity, TargetType, TestCriterion
from core.patcher import PatchConflictError
from core.targets.api_target import SpecError

console = Console()

EXIT_OK, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2
SEVERITY_LABEL = {"CRITICAL": "KRİTİK", "HIGH": "YÜKSEK", "MEDIUM": "ORTA", "LOW": "DÜŞÜK"}
SEVERITY_STYLE = {"CRITICAL": "bold white on red", "HIGH": "bold red", "MEDIUM": "yellow", "LOW": "dim"}


def add_target_arguments(parser: argparse.ArgumentParser) -> None:
    settings = get_settings()
    parser.add_argument("--url", default=f"http://127.0.0.1:{settings.playground_api_port}/openapi.json",
                        help="OpenAPI/Swagger URL or file (API) or page URL (WEB)")
    parser.add_argument("--type", choices=["API", "WEB"], default="API", help="Target type")
    parser.add_argument("--criteria", default=",".join(sorted(c.value for c in ALL_CRITERIA)),
                        help="Comma separated: FUNCTIONAL,BOUNDARY,SECURITY,PERFORMANCE,WEB_VALIDATION")
    parser.add_argument("--sla", type=float, default=settings.default_sla_ms, help="SLA threshold in ms")
    parser.add_argument("--auth", default=None,
                        help='Authorization header for functional checks, e.g. "Bearer <token>"')
    parser.add_argument("--crawl-pages", type=int, default=5, help="WEB: max same-origin pages to crawl")


def build_config(args: argparse.Namespace, with_source: bool) -> HuntConfig:
    try:
        criteria = [TestCriterion(c.strip().upper()) for c in args.criteria.split(",") if c.strip()]
    except ValueError as error:
        raise SystemExit(f"Unknown criterion: {error}") from error
    target_type = TargetType(args.type)
    source = getattr(args, "source", None) if with_source else None
    if with_source and source is None:
        source = playground_source_for(args.url, target_type, get_settings())
    try:
        return HuntConfig(
            target_url=args.url,
            target_type=target_type,
            criteria=criteria,
            sla_threshold_ms=args.sla,
            auth_header=args.auth,
            source_file=source,
            crawl_pages=args.crawl_pages,
        )
    except ValueError as error:
        raise SystemExit(f"Geçersiz yapılandırma: {error}") from error


def execute(session: HuntSession, config: HuntConfig, quiet: bool = False) -> Optional[HuntRun]:
    """Runs a hunt with a progress bar; returns None after printing a friendly error."""
    try:
        if quiet:
            return session.start(config)
        with Progress(TextColumn("[cyan]{task.description}"), BarColumn(), MofNCompleteColumn(),
                      console=console, transient=True) as progress:
            task = progress.add_task("Hedef analiz ediliyor…", total=None)

            def on_result(test, result):
                progress.update(task, description=f"{test.method} {test.endpoint or test.url}"[:60], advance=1)

            def on_plan(tests):
                progress.update(task, total=len(tests), description="Testler koşturuluyor…")

            return session.start(config, on_result=on_result, on_plan=on_plan)
    except httpx.RequestError as error:
        console.print(f"[red]✖ {describe_connection_error(error)}[/red]")
    except httpx.HTTPStatusError as error:
        console.print(f"[red]✖ Hedef HTTP {error.response.status_code} döndürdü:[/red] {error.request.url}")
    except (SpecError, ValueError, OSError) as error:
        console.print(f"[red]✖ {error}[/red]")
    return None


def print_results(run: HuntRun) -> None:
    table = Table(title=f"Test sonuçları · {run.run_id}", header_style="bold magenta", expand=True)
    table.add_column("Durum", no_wrap=True)
    table.add_column("Kriter", no_wrap=True)
    table.add_column("İstek", overflow="fold")
    table.add_column("HTTP", justify="right")
    table.add_column("Süre", justify="right", no_wrap=True)
    results = {r.test_id: r for r in run.results}
    for test in run.tests:
        r = results[test.id]
        status = "[green]✔ GEÇTİ[/green]" if r.passed else "[red]✖ KALDI[/red]"
        table.add_row(status, test.criterion.value, f"{test.method} {test.url}",
                      str(r.status_code or "-"), f"{r.elapsed_ms:.0f} ms")
    console.print(table)
    s = run.summary
    sev = ", ".join(f"{k}: {v}" for k, v in sorted(s.by_severity.items(), key=lambda kv: -Severity(kv[0]).rank))
    console.print(f"[bold]Özet:[/bold] {s.total_tests} test · [green]{s.passed_tests} geçti[/green] · "
                  f"[red]{s.failed_tests} kaldı[/red]" + (f" · {sev}" if sev else ""))


def print_finding(index: int, finding) -> None:
    e, r = finding.evidence, finding.remediation
    sev = e.severity.value
    title_style = {"CRITICAL": "bold red", "HIGH": "bold red", "MEDIUM": "bold yellow"}.get(sev, "bold")
    headline = r.headline if r else e.title
    console.print(f"\n[{SEVERITY_STYLE[sev]}] {SEVERITY_LABEL[sev]} [/] [{title_style}]#{index} {escape(headline)}[/]")
    if r:
        quiet = "" if r.noteworthy else " · [dim]muhtemelen dikkate değer değil[/dim]"
        console.print(f"  [{title_style}]{escape(r.priority)}[/]{quiet}")
    console.print(f"  [dim]{escape(f'{e.method} {e.target_url}')}[/dim]")
    if r:
        console.print(f"  [bold]Çözüm önerisi:[/bold] {escape(r.action)}")
        for number, step in enumerate(r.steps, 1):
            console.print(f"    {number}. {escape(step)}")
        if r.note:
            console.print(f"  [dim]Not: {escape(r.note)}[/dim]")
    else:
        console.print(f"  [bold]Kök neden:[/bold] {escape(e.root_cause_hypothesis)}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_hunt(args: argparse.Namespace) -> int:
    console.print(Panel.fit("[bold cyan]BugHunter AI[/bold cyan] · İnsan onaylı otonom test ajanı\n"
                            "[dim]Test et → Kanıtla → Onay al → Düzelt → Yeniden test et[/dim]",
                            border_style="cyan"))
    config = build_config(args, with_source=True)
    console.print(f"[green]▶ Hedef:[/green] {config.target_url} [dim]({config.target_type.value})[/dim]")
    console.print(f"[green]▶ Kriterler:[/green] {', '.join(c.value for c in config.criteria)}")
    if config.source_file:
        console.print(f"[green]▶ Düzeltme hedefi:[/green] {config.source_file}")

    session = HuntSession()
    run = execute(session, config)
    if run is None:
        return EXIT_ERROR
    print_results(run)
    if not run.findings:
        console.print("[bold green]🎉 Hedef seçilen tüm kriterleri geçti.[/bold green]")
        return EXIT_OK

    for index, finding in enumerate(run.findings, 1):
        print_finding(index, finding)
        proposal = finding.proposal
        if proposal is None or not proposal.patch_available:
            console.print("  [dim]Otomatik düzeltme yok; kanıt raporda.[/dim]")
            continue
        console.print(Panel(Syntax(proposal.unified_diff, "diff", theme="ansi_dark"),
                            title=f"Önerilen düzeltme · {proposal.rule}", subtitle=proposal.explanation))
        if not args.yes and not Confirm.ask("[bold yellow]🛡️ Bu düzeltmeyi uygulamayı onaylıyor musunuz?[/bold yellow]",
                                                  default=False):
            session.reject(run.run_id, proposal.fix_id)
            console.print("  [yellow]✖ Reddedildi; kaynak koda dokunulmadı.[/yellow]")
            continue
        try:
            with console.status("Yama uygulanıyor ve regresyon testleri koşuyor…"):
                report = session.approve(run.run_id, proposal.fix_id)
        except (PatchConflictError, PermissionError, FileNotFoundError) as error:
            console.print(f"  [red]✖ Yama uygulanamadı: {error}[/red]")
            continue
        style = "green" if report.verdict == "CLEAN_FIX" else "red"
        details = "\n".join(report.regressions_detected)
        console.print(Panel(f"{report.summary}\nBaşarılı testler: {report.before_pass_count} → {report.after_pass_count}"
                            + (f"\n{details}" if details else ""),
                            title=f"Regresyon doğrulaması: {report.verdict}", border_style=style))

    path = reporting.write(run, "html", session.settings.reports_dir / f"{run.run_id}.html")
    console.print(f"\n[cyan]✨ Döngü tamamlandı. HTML rapor:[/cyan] {path}")
    return EXIT_OK


def cmd_scan(args: argparse.Namespace) -> int:
    """Headless: no prompts, never modifies code. Exit 1 when findings reach --fail-on."""
    config = build_config(args, with_source=False)
    session = HuntSession()
    run = execute(session, config, quiet=args.quiet)
    if run is None:
        return EXIT_ERROR

    for fmt, target in (("junit", args.junit), ("html", args.html), ("json", args.json)):
        if target:
            path = reporting.write(run, fmt, Path(target))
            if not args.quiet:
                console.print(f"[dim]{fmt.upper()} rapor → {path}[/dim]")
    if not args.quiet:
        print_results(run)
        for index, finding in enumerate(run.findings, 1):
            print_finding(index, finding)

    if args.fail_on == "NEVER":
        return EXIT_OK
    failed = run.has_findings_at_or_above(Severity(args.fail_on))
    if not args.quiet:
        verdict = "[red]BAŞARISIZ[/red]" if failed else "[green]BAŞARILI[/green]"
        console.print(f"\nEşik {args.fail_on}: {verdict}")
    return EXIT_FINDINGS if failed else EXIT_OK


def cmd_runs(args: argparse.Namespace) -> int:
    session = HuntSession()
    runs = session.store.list(limit=args.limit)
    if not runs:
        console.print("Henüz kayıtlı çalışma yok.")
        return EXIT_OK
    table = Table(header_style="bold")
    for column in ("Çalışma", "Tarih (UTC)", "Tür", "Hedef", "Test", "Kalan", "En yüksek"):
        table.add_column(column, overflow="fold")
    for run in runs:
        worst = run.worst_severity()
        table.add_row(run.run_id, f"{run.started_at:%Y-%m-%d %H:%M}", run.config.target_type.value,
                      run.config.target_url, str(run.summary.total_tests), str(run.summary.failed_tests),
                      worst.value if worst else "-")
    console.print(table)
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    session = HuntSession()
    run = session.store.load(args.run_id)
    if run is None:
        console.print(f"[red]Çalışma bulunamadı: {args.run_id}[/red]")
        return EXIT_ERROR
    extension = reporting.FORMATS[args.format][1]
    output = Path(args.output) if args.output else session.settings.reports_dir / f"{run.run_id}.{extension}"
    console.print(f"Rapor yazıldı: {reporting.write(run, args.format, output)}")
    return EXIT_OK


def register(commands) -> None:
    hunt = commands.add_parser("hunt", aliases=["cli"], help="Interactive hunt with human approval of fixes")
    add_target_arguments(hunt)
    hunt.add_argument("--source", default=None, help="Source file fixes are proposed against")
    hunt.add_argument("--yes", action="store_true", default=False,
                      help="Approve every available fix without asking (use only on disposable code)")
    hunt.set_defaults(handler=cmd_hunt)

    scan = commands.add_parser("scan", help="Headless scan for CI: reports + exit code, never patches code")
    add_target_arguments(scan)
    scan.add_argument("--junit", help="Write JUnit XML to this path")
    scan.add_argument("--html", help="Write the HTML report to this path")
    scan.add_argument("--json", help="Write the JSON report to this path")
    scan.add_argument("--fail-on", default="HIGH", choices=["CRITICAL", "HIGH", "MEDIUM", "LOW", "NEVER"],
                      help="Exit 1 when a finding has at least this severity (default: HIGH)")
    scan.add_argument("--quiet", action="store_true", help="Only print errors")
    scan.set_defaults(handler=cmd_scan)

    runs = commands.add_parser("runs", help="List stored runs")
    runs.add_argument("--limit", type=int, default=20)
    runs.set_defaults(handler=cmd_runs)

    report = commands.add_parser("report", help="Export a stored run")
    report.add_argument("run_id")
    report.add_argument("--format", choices=sorted(reporting.FORMATS), default="html")
    report.add_argument("-o", "--output", help="Output path (default: reports/<run_id>.<ext>)")
    report.set_defaults(handler=cmd_report)


def run_cli(argv: Optional[list] = None) -> int:
    """Backwards-compatible entry point: `python cli.py --url ...` runs an interactive hunt."""
    parser = argparse.ArgumentParser(description="BugHunter AI CLI")
    add_target_arguments(parser)
    parser.add_argument("--source", default=None)
    parser.add_argument("--yes", action="store_true", default=False)
    return cmd_hunt(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(run_cli())
