"""
BugHunter AI — Rich Interactive CLI.
Provides a terminal interface for running autonomous tests, inspecting defects,
reviewing Unified Diffs, and granting human-in-the-loop approval.
"""

import argparse
import os
import sys
from typing import Set

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.prompt import Confirm

from core.analyzer import BugAnalyzer
from core.generator import TestGenerator
from core.models import TargetType, TestCriterion
from core.patcher import CodePatcher, HumanApprovalGate
from core.runner import TestRunner
from core.targets.api_target import ApiTargetParser
from core.targets.web_target import WebTargetParser
from core.verifier import RegressionVerifier

console = Console()


def display_banner():
    banner_text = """[bold cyan]
██████╗ ██╗   ██╗ ██████╗ ██╗  ██╗██╗   ██╗███╗   ██╗████████╗███████╗██████╗ 
██╔══██╗██║   ██║██╔════╝ ██║  ██║██║   ██║████╗  ██║╚══██╔══╝██╔════╝██╔══██╗
██████╔╝██║   ██║██║  ███╗███████║██║   ██║██╔██╗ ██║   ██║   █████╗  ██████╔╝
██╔══██╗██║   ██║██║   ██║██╔══██║██║   ██║██║╚██╗██║   ██║   ██╔══╝  ██╔══██╗
██████╔╝╚██████╔╝╚██████╔╝██║  ██║╚██████╔╝██║ ╚████║   ██║   ███████╗██║  ██║
╚═════╝  ╚═════╝  ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝   ╚═╝   ╚══════╝╚═╝  ╚═╝
[/bold cyan]
[bold white]İnsan Onaylı Otonom Yazılım Test ve Hata Analiz Ajanı (v1.0.0)[/bold white]
[italic dim]Test Et ➔ Araştır ➔ Kanıtla ➔ Onay Al ➔ Düzelt ➔ Yeniden Test Et[/italic dim]
"""
    console.print(Panel(banner_text, border_style="cyan"))


def run_cli():
    display_banner()

    parser = argparse.ArgumentParser(description="BugHunter AI CLI Runner")
    parser.add_argument("--url", type=str, default="http://127.0.0.1:8001/openapi.json", help="Target URL or spec")
    parser.add_argument("--type", type=str, choices=["API", "WEB"], default="API", help="Target type (API or WEB)")
    parser.add_argument("--sla", type=float, default=500.0, help="SLA response time threshold in ms")
    args = parser.parse_args()

    target_url = args.url
    target_type = TargetType[args.type]

    console.print(f"[bold green]▶ Hedef:[/bold green] {target_url} [dim]({target_type.value})[/dim]")
    console.print(f"[bold green]▶ Kriterler:[/bold green] FUNCTIONAL, BOUNDARY, SECURITY, PERFORMANCE, WEB_VALIDATION")
    console.print(f"[bold green]▶ SLA Eşiği:[/bold green] {args.sla} ms\n")

    # Step 1: Parse
    console.print("[yellow][1/6] 🔍 Hedef analiz ediliyor ve şema okunuyor...[/yellow]")
    if target_type == TargetType.API:
        api_parser = ApiTargetParser(target_url)
        endpoints = api_parser.parse()
        base_url = target_url.rsplit("/", 1)[0]
        generator = TestGenerator(base_url=base_url, sla_threshold_ms=args.sla)
        tests = generator.generate_api_tests(endpoints)
    else:
        web_parser = WebTargetParser(target_url)
        page_spec = web_parser.parse()
        generator = TestGenerator(base_url=target_url, sla_threshold_ms=args.sla)
        tests = generator.generate_web_tests(page_spec)

    console.print(f"[green]✔ [bold]{len(tests)}[/bold] otonom test senaryosu üretildi.[/green]\n")

    # Step 2: Run Tests
    console.print("[yellow][2/6] 🚀 Testler hedefe karşı koşturuluyor...[/yellow]")
    runner = TestRunner()
    results = runner.run_suite(tests)

    # Display Test Results Table
    results_table = Table(title="Test Koşum Sonuçları", show_header=True, header_style="bold magenta")
    results_table.add_column("Durum", width=12)
    results_table.add_column("Test ID", width=28)
    results_table.add_column("Metot & URL", width=34)
    results_table.add_column("Yanıt", width=8)
    results_table.add_column("Süre", width=10)

    test_map = {t.id: t for t in tests}
    failed_tests = []

    for r in results:
        t = test_map[r.test_id]
        status_str = "[bold green]✔ GEÇTİ[/bold green]" if r.passed else "[bold red]✖ HATA[/bold red]"
        results_table.add_row(
            status_str,
            r.test_id,
            f"{t.method} {t.url[:30]}",
            str(r.status_code or "-"),
            f"{r.elapsed_ms:.1f}ms"
        )
        if not r.passed:
            failed_tests.append((t, r))

    console.print(results_table)

    pass_count = sum(1 for r in results if r.passed)
    fail_count = len(failed_tests)
    console.print(f"\n[bold]Özet:[/bold] [green]{pass_count} Başarılı[/green], [red]{fail_count} Başarısız[/red]\n")

    if not failed_tests:
        console.print("[bold green]🎉 Harika! Hedef sistem belirlenen tüm kriterleri başarıyla geçti.[/bold green]")
        return

    # Step 3: Analyze & Evidence Collection
    console.print("[yellow][3/6] 🔬 Başarısız testler izole ediliyor ve kanıt toplanıyor...[/yellow]")
    analyzer = BugAnalyzer()
    gate = HumanApprovalGate()
    patcher = CodePatcher(approval_gate=gate)
    verifier = RegressionVerifier()

    target_file = os.path.abspath("playground/sample_api.py" if target_type == TargetType.API else "playground/sample_web.py")

    for idx, (failed_test, failed_result) in enumerate(failed_tests, 1):
        evidence = analyzer.analyze_failure(failed_test, failed_result)
        console.print(f"\n[bold red]━━━ Hata #{idx}: {evidence.title} ({evidence.severity}) ━━━[/bold red]")
        console.print(f"[bold]Kök Neden Hipotezi:[/bold] {evidence.root_cause_hypothesis}")
        console.print(f"[bold]Beklenen:[/bold] {evidence.expected_behavior}")
        console.print(f"[bold]Gerçekleşen:[/bold] {evidence.actual_behavior}")

        # Step 4: Propose Fix
        proposal = patcher.generate_fix(evidence, target_file=target_file)
        console.print(f"\n[bold cyan]📝 Önerilen Kod Düzeltmesi (Unified Diff) ➔ {target_file}[/bold cyan]")
        syntax = Syntax(proposal.unified_diff, "diff", theme="monokai", line_numbers=False)
        console.print(Panel(syntax, title=f"Fix Proposal {proposal.fix_id}"))

        # Step 5: STRICT HUMAN APPROVAL GATE
        console.print("\n[bold yellow]🛡️ [HUMAN-IN-THE-LOOP ON AY KİLİDİ][/bold yellow]")
        approved = Confirm.ask(f"[bold]Bu kod düzeltmesini {os.path.basename(target_file)} dosyasına uygulamayı onaylıyor musunuz?[/bold]", default=True)

        if approved:
            gate.approve_fix(proposal.fix_id)
            gate.apply_patch(proposal.fix_id, create_backup=True)
            console.print(f"[green]✔ Düzeltme uygulandı! (Yedek: {target_file}.bughunter.bak)[/green]")

            # Step 6: Regression Verification
            console.print("[yellow][6/6] 🔄 Regresyon doğrulaması koşturuluyor...[/yellow]")
            report = verifier.verify(
                runner=runner,
                client=None,
                all_tests=tests,
                target_test=failed_test,
                before_results=results,
                proposal=proposal,
                gate=gate,
                auto_rollback_on_regression=True
            )

            if report.verdict == "CLEAN_FIX":
                console.print(Panel(f"[bold green]{report.summary}[/bold green]\n"
                                    f"Başarılı Testler: {report.before_pass_count} ➔ [bold green]{report.after_pass_count}[/bold green]\n"
                                    f"Regresyon / Yan Etki: [bold green]0 (Sıfır)[/bold green]",
                                    title="Regresyon Doğrulama Raporu: TEMİZ", border_style="green"))
            else:
                console.print(Panel(f"[bold red]{report.summary}[/bold red]\n"
                                    f"Yan Etkiler: {', '.join(report.regressions_detected)}",
                                    title="Regresyon Tespit Edildi", border_style="red"))
        else:
            gate.reject_fix(proposal.fix_id)
            console.print("[yellow]✖ Düzeltme kullanıcı tarafından reddedildi. Kaynak koda dokunulmadı.[/yellow]")

    console.print("\n[bold cyan]✨ BugHunter döngüsü tamamlandı.[/bold cyan]")


if __name__ == "__main__":
    run_cli()
