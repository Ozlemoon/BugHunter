"""Console helpers shared by every entry point (including spawned child processes)."""

import sys


def configure_utf8() -> None:
    """Windows consoles and pipes default to a legacy code page and crash on emoji / Turkish text."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
