"""Keeps credentials out of anything written to disk or sent back to a browser."""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict

if TYPE_CHECKING:
    from core.engine import HuntRun

SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "cookie", "x-api-key", "x-auth-token"}
REDACTED = "<redacted>"
AUTH_ENV_VAR = "BUGHUNTER_AUTH_HEADER"


def is_sensitive(name: str) -> bool:
    return name.lower() in SENSITIVE_HEADERS


def redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    return {name: REDACTED if is_sensitive(name) else value for name, value in headers.items()}


def redact_run(run: HuntRun) -> HuntRun:
    """A copy of the run that is safe to persist or export (live run keeps its secrets in memory)."""
    copy = run.model_copy(deep=True)
    if copy.config.auth_header:
        copy.config.auth_header = REDACTED
    for test in copy.tests:
        test.headers = redact_headers(test.headers)
    return copy
