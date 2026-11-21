"""
BugHunter AI Web Dashboard.

FastAPI backend for the dashboard: start hunts, browse run history, download
reports, and approve / reject / roll back fixes through the human approval gate.
All work is delegated to core.engine.HuntSession.
"""

from __future__ import annotations

import hmac
import logging
from pathlib import Path
from typing import Any, Dict

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from core import reporting
from core.config import get_settings
from core.engine import (
    HuntConfig,
    HuntRun,
    HuntSession,
    describe_connection_error,
    playground_source_for,
    resolve_source_file,
)
from core.generator import ALL_CRITERIA
from core.models import TargetType
from core.patcher import PatchConflictError
from core.targets.api_target import SpecError

__version__ = "1.1.0"

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
logger = logging.getLogger(__name__)

app = FastAPI(
    title="BugHunter AI Web Dashboard",
    version=__version__,
    description="Human-in-the-loop autonomous API & web testing: evidence, safe patches, regression checks.",
)

session = HuntSession()


def get_session() -> HuntSession:
    return session  # module attribute, so tests can swap it


# ---------------------------------------------------------------------------
# Security & error handling
# ---------------------------------------------------------------------------

@app.middleware("http")
async def security_middleware(request: Request, call_next):
    token = get_settings().api_token
    if token and request.url.path.startswith("/api/"):
        supplied = request.headers.get("x-bughunter-token", "")
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            return JSONResponse(status_code=401, content={"detail": "API token required."})
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@app.exception_handler(httpx.RequestError)
async def target_connection_error(request: Request, error: httpx.RequestError):
    return JSONResponse(status_code=502, content={"detail": describe_connection_error(error)})


@app.exception_handler(httpx.HTTPStatusError)
async def target_http_error(request: Request, error: httpx.HTTPStatusError):
    return JSONResponse(status_code=502, content={
        "detail": f"{error.request.url} adresi HTTP {error.response.status_code} döndürdü. "
                  "Adresi ve erişim gereksinimlerini (ör. Authorization) kontrol edin."
    })


@app.exception_handler(SpecError)
async def spec_error(request: Request, error: SpecError):
    return JSONResponse(status_code=422, content={"detail": f"OpenAPI belgesi okunamadı: {error}"})


@app.exception_handler(PatchConflictError)
async def patch_conflict(request: Request, error: PatchConflictError):
    return JSONResponse(status_code=409, content={"detail": str(error)})


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def run_payload(run: HuntRun) -> Dict[str, Any]:
    results = {r.test_id: r for r in run.results}
    return {
        "status": "success",
        "run_id": run.run_id,
        "started_at": run.started_at.isoformat(),
        "config": run.config.model_dump(mode="json", exclude={"auth_header"}),
        "base_url": run.base_url,
        "summary": {**run.summary.model_dump(), "target_type": run.config.target_type.value},
        "findings": [f.model_dump(mode="json") for f in run.findings],
        "results": [
            {
                "test_id": t.id,
                "title": t.title,
                "criterion": t.criterion.value,
                "method": t.method,
                "url": t.url,
                "passed": results[t.id].passed,
                "status_code": results[t.id].status_code,
                "elapsed_ms": results[t.id].elapsed_ms,
                "failed_assertions": results[t.id].failed_assertions,
            }
            for t in run.tests if t.id in results
        ],
    }


def run_listing(run: HuntRun) -> Dict[str, Any]:
    worst = run.worst_severity()
    return {
        "run_id": run.run_id,
        "started_at": run.started_at.isoformat(),
        "target_type": run.config.target_type.value,
        "target_url": run.config.target_url,
        "total_tests": run.summary.total_tests,
        "failed_tests": run.summary.failed_tests,
        "worst_severity": worst.value if worst else None,
    }


def _load(run_id: str) -> HuntRun:
    run = get_session().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Çalışma bulunamadı.")
    return run


def _fix_action(action, run_id: str, fix_id: str):
    try:
        return action(run_id, fix_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error).strip("'\"")) from error
    except (OSError, ValueError) as error:  # permission, missing file, nothing to roll back
        raise HTTPException(status_code=409, detail=str(error)) from error


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"version": __version__})


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "version": __version__}


@app.get("/api/config")
async def dashboard_config():
    settings = get_settings()
    return {
        "version": __version__,
        "criteria": sorted(c.value for c in ALL_CRITERIA),
        "default_sla_ms": settings.default_sla_ms,
        "demo_targets": [
            {"label": "Demo API", "target_type": "API",
             "target_url": f"http://127.0.0.1:{settings.playground_api_port}/openapi.json",
             "auth_header": "Bearer admin-token", "source_file": "playground/sample_api.py"},
            {"label": "Demo web", "target_type": "WEB",
             "target_url": f"http://127.0.0.1:{settings.playground_web_port}/",
             "auth_header": "", "source_file": "playground/sample_web.py"},
        ],
    }


@app.post("/api/runs")
def start_run(config: HuntConfig):
    """Plan → run → analyse → draft fixes (never applied without approval)."""
    current = get_session()
    if config.source_file is None:
        demo = playground_source_for(config.target_url, config.target_type, current.settings)
        try:
            resolve_source_file(demo, current.settings)
            config = config.model_copy(update={"source_file": demo})
        except ValueError:
            pass  # auto-detected demo file not patchable here: collect evidence only
    try:
        run = current.start(config)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return run_payload(run)


class LegacyHuntRequest(BaseModel):
    target_url: str
    target_type: TargetType = TargetType.API
    criteria: list = Field(default_factory=lambda: sorted(c.value for c in ALL_CRITERIA))
    sla_threshold_ms: float = 500.0


@app.post("/api/run-hunt", deprecated=True)
def run_hunt(payload: LegacyHuntRequest):
    """Backwards-compatible alias of POST /api/runs."""
    known = [c for c in payload.criteria if c in {x.value for x in ALL_CRITERIA}]
    return start_run(HuntConfig(target_url=payload.target_url, target_type=payload.target_type,
                                criteria=known, sla_threshold_ms=payload.sla_threshold_ms))


@app.get("/api/runs")
def list_runs(limit: int = 20):
    return {"runs": [run_listing(run) for run in get_session().store.list(limit=max(1, min(limit, 100)))]}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    return run_payload(_load(run_id))


@app.get("/api/runs/{run_id}/report.{fmt}")
def download_report(run_id: str, fmt: str):
    names = {"json": "json", "xml": "junit", "html": "html"}
    if fmt not in names:
        raise HTTPException(status_code=404, detail="Desteklenen biçimler: json, xml, html.")
    run = _load(run_id)
    media_type = reporting.FORMATS[names[fmt]][0]
    disposition = "inline" if fmt == "html" else "attachment"
    return Response(
        content=reporting.render(run, names[fmt]),
        media_type=media_type,
        headers={"Content-Disposition": f'{disposition}; filename="bughunter-{run.run_id}.{fmt}"'},
    )


@app.post("/api/runs/{run_id}/fixes/{fix_id}/approve")
def approve_fix(run_id: str, fix_id: str):
    """Human approval: apply the patch, then re-run the whole suite for regressions."""
    report = _fix_action(get_session().approve, run_id, fix_id)
    return {"status": "success", "fix_id": fix_id, "report": report.model_dump(mode="json"),
            "proposal": get_session().get_run(run_id).finding_for_fix(fix_id).proposal.model_dump(mode="json")}


@app.post("/api/runs/{run_id}/fixes/{fix_id}/reject")
def reject_fix(run_id: str, fix_id: str):
    proposal = _fix_action(get_session().reject, run_id, fix_id)
    return {"status": "rejected", "fix_id": fix_id, "proposal": proposal.model_dump(mode="json")}


@app.post("/api/runs/{run_id}/fixes/{fix_id}/rollback")
def rollback_fix(run_id: str, fix_id: str):
    proposal = _fix_action(get_session().rollback, run_id, fix_id)
    return {"status": "rolled_back", "fix_id": fix_id, "proposal": proposal.model_dump(mode="json")}


class RagQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


@app.post("/api/rag/ask")
def ask_documents(payload: RagQuestion):
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="Bir soru yazın.")
    try:
        from core.rag import DocumentIndex
        from core.rag_answer import answer_question

        settings = get_settings()
        store = DocumentIndex(settings.rag_db)
        return answer_question(store, question, top_k=6, top_n=3, model=settings.rag_chat_model)
    except ImportError as error:
        raise HTTPException(status_code=503, detail="RAG bağımlılıkları kurulu değil.") from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=503,
            detail="Belge yanıtı alınamadı. Ollama'nın çalıştığını kontrol edip tekrar deneyin.",
        ) from error
