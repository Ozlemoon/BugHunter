"""
BugHunter AI — unified command line.

    python main.py demo                     # playground targets + dashboard
    python main.py web                      # dashboard only (http://127.0.0.1:8000)
    python main.py playground [api|web]     # deliberately buggy demo targets
    python main.py hunt --url ... --type API          # interactive, human-approved fixes
    python main.py scan --url ... --junit out.xml     # headless CI scan (exit code 1 on findings)
    python main.py runs | report <run_id> --format html
    python main.py rag index|search|ask ...           # local document Q&A (needs Ollama)
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing
import sys
from typing import Optional

from core.config import get_settings
from core.console import configure_utf8

__version__ = "1.1.0"


def start_web(host: Optional[str] = None, port: Optional[int] = None, reload: bool = False) -> None:
    import uvicorn

    configure_utf8()
    settings = get_settings()
    host, port = host or settings.host, port or settings.port
    print(f"\n🚀 BugHunter AI paneli: http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}\n")
    uvicorn.run("web.app:app", host=host, port=port, log_level=settings.log_level.lower(), reload=reload,
                reload_dirs=["web", "core"] if reload else None)


def start_playground(which: str = "all", reload: bool = True) -> int:
    from playground.runner import run_all, run_api, run_web

    if which == "api":
        run_api(reload=reload)
    elif which == "web":
        run_web(reload=reload)
    else:
        return run_all(reload=reload)
    return 0


def start_demo(host: Optional[str] = None, port: Optional[int] = None) -> None:
    settings = get_settings()
    print("=" * 70)
    print("🎯 BugHunter AI — uçtan uca demo")
    print(f"  Örnek API:      http://127.0.0.1:{settings.playground_api_port}/openapi.json")
    print(f"  Örnek web:      http://127.0.0.1:{settings.playground_web_port}")
    print(f"  Kontrol paneli: http://127.0.0.1:{port or settings.port}")
    print("=" * 70)
    targets = multiprocessing.Process(target=start_playground, daemon=False)
    targets.start()
    try:
        start_web(host, port)
    finally:
        targets.terminate()
        targets.join(timeout=5)
        print("\n🛑 Demo durduruldu.")


def build_parser() -> argparse.ArgumentParser:
    import cli

    parser = argparse.ArgumentParser(prog="bughunter", description="Human-in-the-loop autonomous testing agent")
    parser.add_argument("--version", action="version", version=f"BugHunter AI {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    web = commands.add_parser("web", help="Start the web dashboard")
    web.add_argument("--host")
    web.add_argument("--port", type=int)
    web.add_argument("--reload", action="store_true", help="Auto-reload on code changes (development)")
    web.set_defaults(handler=lambda a: start_web(a.host, a.port, a.reload))

    playground = commands.add_parser("playground", aliases=["targets"], help="Start the buggy demo targets")
    playground.add_argument("which", nargs="?", choices=["all", "api", "web"], default="all")
    playground.add_argument("--no-reload", action="store_true", help="Do not restart targets after patches")
    playground.set_defaults(handler=lambda a: start_playground(a.which, not a.no_reload))

    demo = commands.add_parser("demo", help="Start demo targets and the dashboard together")
    demo.add_argument("--host")
    demo.add_argument("--port", type=int)
    demo.set_defaults(handler=lambda a: start_demo(a.host, a.port))

    cli.register(commands)

    rag = commands.add_parser("rag", help="Local document Q&A (index / search / ask)", add_help=False)
    rag.add_argument("rag_args", nargs=argparse.REMAINDER)
    rag.set_defaults(handler=None)
    return parser


def main(argv: Optional[list] = None) -> int:
    configure_utf8()
    argv = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=get_settings().log_level, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is noise

    if argv and argv[0] == "rag":
        from core.rag import main as rag_main

        sys.argv = ["bughunter rag", *argv[1:]]
        rag_main()
        return 0

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 0
    result = args.handler(args)
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
