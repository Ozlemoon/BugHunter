"""
Central runtime configuration.

Every value can be overridden with an environment variable (or a `.env` file in
the working directory), so the same code runs on a laptop, in Docker and in CI
without edits. See `.env.example` for the full list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, existing env vars win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(name: str, default: str) -> str:
    return os.environ.get(f"BUGHUNTER_{name}", default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError as error:
        raise ValueError(f"BUGHUNTER_{name} must be a number") from error


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError as error:
        raise ValueError(f"BUGHUNTER_{name} must be an integer") from error


def _resolve(path: str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    playground_api_port: int = 8001
    playground_web_port: int = 8002
    log_level: str = "INFO"

    # Test execution
    request_timeout: float = 15.0
    default_sla_ms: float = 500.0
    # Seconds to wait after a patch so an auto-reloading target can restart.
    patch_settle_seconds: float = 1.5
    patch_ready_timeout: float = 15.0
    # Source files may only be patched when they live under one of these roots.
    patch_roots: tuple[Path, ...] = field(default_factory=lambda: (PROJECT_ROOT,))

    # Storage
    reports_dir: Path = PROJECT_ROOT / "reports"

    # Local RAG (Ollama + Chroma)
    ollama_url: str = "http://127.0.0.1:11434"
    rag_db: Path = PROJECT_ROOT / "run_artifacts" / "rag"
    rag_chat_model: str = "qwen3.5"
    rag_embed_model: str = "embeddinggemma"

    @classmethod
    def from_env(cls) -> Settings:
        _load_dotenv(Path.cwd() / ".env")
        roots = _env("PATCH_ROOTS", "")
        return cls(
            host=_env("HOST", cls.host),
            port=_env_int("PORT", cls.port),
            playground_api_port=_env_int("PLAYGROUND_API_PORT", cls.playground_api_port),
            playground_web_port=_env_int("PLAYGROUND_WEB_PORT", cls.playground_web_port),
            log_level=_env("LOG_LEVEL", cls.log_level).upper(),
            request_timeout=_env_float("REQUEST_TIMEOUT", cls.request_timeout),
            default_sla_ms=_env_float("DEFAULT_SLA_MS", cls.default_sla_ms),
            patch_settle_seconds=_env_float("PATCH_SETTLE_SECONDS", cls.patch_settle_seconds),
            patch_ready_timeout=_env_float("PATCH_READY_TIMEOUT", cls.patch_ready_timeout),
            patch_roots=tuple(_resolve(p) for p in roots.split(os.pathsep) if p) or (PROJECT_ROOT,),
            reports_dir=_resolve(_env("REPORTS_DIR", "reports")),
            ollama_url=_env("OLLAMA_URL", cls.ollama_url).rstrip("/"),
            rag_db=_resolve(_env("RAG_DB", "run_artifacts/rag")),
            rag_chat_model=_env("RAG_CHAT_MODEL", cls.rag_chat_model),
            rag_embed_model=_env("RAG_EMBED_MODEL", cls.rag_embed_model),
        )

    def is_patchable(self, path: str | Path) -> bool:
        """True when `path` resolves inside one of the allowed patch roots."""
        resolved = Path(path).resolve()
        return any(resolved.is_relative_to(root.resolve()) for root in self.patch_roots)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
