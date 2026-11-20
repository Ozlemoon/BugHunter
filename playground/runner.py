"""
Playground Runner:
Starts the Sample API, the Sample Web App, or both.

Both targets run with auto-reload watching the playground folder, so a fix
approved in BugHunter is live within a second and the regression check
re-tests the patched code (reload also resets the in-memory demo data).
"""

import multiprocessing
import sys
from pathlib import Path

import uvicorn

from core.config import get_settings

PLAYGROUND_DIR = str(Path(__file__).resolve().parent)


def _serve(app_path: str, port: int, reload: bool) -> None:
    settings = get_settings()
    uvicorn.run(
        app_path,
        host=settings.host,
        port=port,
        log_level="warning",
        reload=reload,
        reload_dirs=[PLAYGROUND_DIR] if reload else None,
        reload_includes=["*.py"] if reload else None,
    )


def run_api(port: int | None = None, reload: bool = True) -> None:
    port = port or get_settings().playground_api_port
    print(f"🚀 Sample API target on http://127.0.0.1:{port}  (docs: /docs)")
    _serve("playground.sample_api:app", port, reload)


def run_web(port: int | None = None, reload: bool = True) -> None:
    port = port or get_settings().playground_web_port
    print(f"🌐 Sample Web App target on http://127.0.0.1:{port}")
    _serve("playground.sample_web:app", port, reload)


def run_all(reload: bool = True) -> None:
    processes = [
        multiprocessing.Process(target=run_api, kwargs={"reload": reload}),
        multiprocessing.Process(target=run_web, kwargs={"reload": reload}),
    ]
    for process in processes:
        process.start()
    print("✨ Playground targets are running. Press Ctrl+C to stop.")
    try:
        for process in processes:
            process.join()
    except KeyboardInterrupt:
        for process in processes:
            process.terminate()
        print("\n🛑 Stopped playground targets.")


if __name__ == "__main__":
    choice = sys.argv[1] if len(sys.argv) > 1 else "all"
    {"api": run_api, "web": run_web}.get(choice, run_all)()
