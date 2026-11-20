"""Shared helpers for tests that exercise the playground targets in-process."""

import importlib.util
import sys
import uuid
from pathlib import Path

PLAYGROUND = Path(__file__).resolve().parent.parent / "playground"
API_BASE = "http://127.0.0.1:8001"
WEB_BASE = "http://127.0.0.1:8002"
AUTH = {"Authorization": "Bearer admin-token"}


def load_app(path: Path):
    """Imports a fresh copy of a playground module so in-memory state never leaks between tests."""
    name = f"bh_playground_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.app
