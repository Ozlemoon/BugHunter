"""
BugHunter AI Web Dashboard.

FastAPI backend for the dashboard: start hunts, browse run history, download
reports, and approve / reject / roll back fixes through the human approval gate.
All work is delegated to core.engine.HuntSession.
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from core import mailer, reporting
from core.auth import SESSION_LIFETIME, AuthError, User
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

SESSION_COOKIE = "bughunter_session"


def current_user(request: Request) -> Optional[User]:
    return get_session().users.user_for_session(request.cookies.get(SESSION_COOKIE))


def require_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Oturum açmanız gerekiyor.",
                            headers={"X-BugHunter-Login": "required"})
    return user


def _cross_site(request: Request) -> bool:
    """A state-changing request sent by a page on another site (CSRF)."""
    origin = request.headers.get("origin")
    if origin is None:
        return False
    if origin == "null":
        return True
    return urlsplit(origin).netloc.lower() != (request.headers.get("host") or "").lower()


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS") and _cross_site(request):
        return JSONResponse(status_code=403, content={"detail": "Başka bir siteden gönderilen istek reddedildi."})
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
        "recommendations": [r.model_dump(mode="json") for r in run.recommendations],
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


def _load(run_id: str, user: User) -> HuntRun:
    run = get_session().get_run(run_id, owner_id=user.id)
    if run is None:
        raise HTTPException(status_code=404, detail="Çalışma bulunamadı.")
    return run


def _fix_action(action, run_id: str, fix_id: str, user: User):
    _load(run_id, user)  # 404 unless this user owns the run
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
def dashboard(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request=request, name="dashboard.html",
                                      context={"version": __version__, "username": user.username})


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    first_user = get_session().users.count() == 0
    return templates.TemplateResponse(request=request, name="login.html", context={
        "version": __version__,
        "allow_signup": get_settings().allow_signup or first_user,
        "first_user": first_user,
    })


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


def _logged_in(request: Request, user: User) -> JSONResponse:
    token = get_session().users.create_session(user)
    response = JSONResponse({"status": "success", "username": user.username})
    response.set_cookie(SESSION_COOKIE, token, max_age=int(SESSION_LIFETIME.total_seconds()),
                        httponly=True, samesite="lax", secure=request.url.scheme == "https", path="/")
    return response


@app.post("/api/auth/login")
def login(request: Request, payload: Credentials):
    try:
        user = get_session().users.authenticate(payload.username, payload.password)
    except AuthError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    return _logged_in(request, user)


@app.post("/api/auth/register")
def register(request: Request, payload: Credentials):
    current = get_session()
    first_user = current.users.count() == 0
    if not (get_settings().allow_signup or first_user):
        raise HTTPException(status_code=403, detail="Yeni hesap oluşturma kapalı. Yöneticinizden hesap isteyin.")
    try:
        user = current.users.create(payload.username, payload.password)
    except AuthError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if first_user:
        # Tests run before accounts existed (and from the CLI) belong to the first account.
        claimed = current.store.assign_unowned(user.id)
        logger.info("First account %s now owns %d earlier runs", user.username, claimed)
    return _logged_in(request, user)


@app.post("/api/auth/logout")
def logout(request: Request):
    get_session().users.end_session(request.cookies.get(SESSION_COOKIE))
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/auth/me")
def me(user: User = Depends(require_user)):
    return {"username": user.username}


@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    svg = templates.get_template("_logo.html").module.logo(64, "fav")
    return Response(content=str(svg), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "version": __version__}


@app.get("/api/config")
def dashboard_config(user: User = Depends(require_user)):
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
def start_run(config: HuntConfig, user: User = Depends(require_user)):
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
        run = current.start(config, owner_id=user.id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return run_payload(run)


class LegacyHuntRequest(BaseModel):
    target_url: str
    target_type: TargetType = TargetType.API
    criteria: list = Field(default_factory=lambda: sorted(c.value for c in ALL_CRITERIA))
    sla_threshold_ms: float = 500.0


@app.post("/api/run-hunt", deprecated=True)
def run_hunt(payload: LegacyHuntRequest, user: User = Depends(require_user)):
    """Backwards-compatible alias of POST /api/runs."""
    known = [c for c in payload.criteria if c in {x.value for x in ALL_CRITERIA}]
    return start_run(HuntConfig(target_url=payload.target_url, target_type=payload.target_type,
                                criteria=known, sla_threshold_ms=payload.sla_threshold_ms), user)


@app.get("/api/runs")
def list_runs(limit: int = 20, user: User = Depends(require_user)):
    """The signed-in user's earlier runs ("Önceki testlerim"), newest first."""
    runs = get_session().store.list(limit=max(1, min(limit, 100)), owner_id=user.id)
    return {"runs": [run_listing(run) for run in runs]}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, user: User = Depends(require_user)):
    return run_payload(_load(run_id, user))


@app.get("/api/runs/{run_id}/report.{fmt}")
def download_report(run_id: str, fmt: str, user: User = Depends(require_user)):
    names = {"json": "json", "xml": "junit", "html": "html"}
    if fmt not in names:
        raise HTTPException(status_code=404, detail="Desteklenen biçimler: json, xml, html.")
    run = _load(run_id, user)
    media_type = reporting.FORMATS[names[fmt]][0]
    disposition = "inline" if fmt == "html" else "attachment"
    return Response(
        content=reporting.render(run, names[fmt]),
        media_type=media_type,
        headers={"Content-Disposition": f'{disposition}; filename="bughunter-{run.run_id}.{fmt}"'},
    )


@app.post("/api/runs/{run_id}/fixes/{fix_id}/approve")
def approve_fix(run_id: str, fix_id: str, user: User = Depends(require_user)):
    """Human approval: apply the patch, then re-run the whole suite for regressions."""
    report = _fix_action(get_session().approve, run_id, fix_id, user)
    return {"status": "success", "fix_id": fix_id, "report": report.model_dump(mode="json"),
            "proposal": get_session().get_run(run_id).finding_for_fix(fix_id).proposal.model_dump(mode="json")}


@app.post("/api/runs/{run_id}/fixes/{fix_id}/reject")
def reject_fix(run_id: str, fix_id: str, user: User = Depends(require_user)):
    proposal = _fix_action(get_session().reject, run_id, fix_id, user)
    return {"status": "rejected", "fix_id": fix_id, "proposal": proposal.model_dump(mode="json")}


@app.post("/api/runs/{run_id}/fixes/{fix_id}/rollback")
def rollback_fix(run_id: str, fix_id: str, user: User = Depends(require_user)):
    proposal = _fix_action(get_session().rollback, run_id, fix_id, user)
    return {"status": "rolled_back", "fix_id": fix_id, "proposal": proposal.model_dump(mode="json")}


class EmailRequest(BaseModel):
    recipients: Union[str, List[str]] = Field(description="Addresses, comma separated or as a list")
    note: Optional[str] = Field(default=None, max_length=mailer.MAX_NOTE_LENGTH)


MAIL_LIMIT = 10           # e-mails ...
MAIL_WINDOW_SECONDS = 600  # ... per user per 10 minutes
_mail_log: Dict[int, List[float]] = {}
_mail_lock = threading.Lock()


def _check_mail_rate(user: User) -> None:
    with _mail_lock:
        now = time.monotonic()
        recent = [t for t in _mail_log.get(user.id, []) if now - t < MAIL_WINDOW_SECONDS]
        if len(recent) >= MAIL_LIMIT:
            raise HTTPException(status_code=429, detail="Kısa sürede çok fazla e-posta gönderildi. Birkaç dakika sonra tekrar deneyin.")
        recent.append(now)
        _mail_log[user.id] = recent


@app.get("/api/mail/status")
def mail_status(user: User = Depends(require_user)):
    settings = get_settings()
    return {"configured": settings.mail_configured, "sender": settings.smtp_from or None,
            "report_base_url": settings.base_url, "max_recipients": mailer.MAX_RECIPIENTS}


@app.post("/api/runs/{run_id}/email")
def email_report(run_id: str, payload: EmailRequest, user: User = Depends(require_user)):
    """E-mail the run's summary with a "Raporu görüntüle" link and the HTML report attached."""
    run = _load(run_id, user)
    settings = get_settings()
    try:
        recipients = mailer.parse_recipients(payload.recipients)
    except mailer.MailError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if not settings.mail_configured:
        raise HTTPException(status_code=503, detail=mailer.NOT_CONFIGURED)
    _check_mail_rate(user)
    store = get_session().store
    token = store.create_share(run.run_id, days=settings.share_link_days)
    report_url = f"{settings.base_url}/r/{token}"
    message = mailer.build_message(run, recipients, settings.smtp_from, report_url,
                                   sent_by=user.username, note=payload.note)
    try:
        mailer.send(message, settings)
    except mailer.MailError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    logger.info("User %s e-mailed run %s to %d recipient(s)", user.username, run.run_id, len(recipients))
    return {"status": "sent", "recipients": recipients, "report_url": report_url}


@app.get("/r/{token}", response_class=HTMLResponse, include_in_schema=False)
def shared_report(token: str):
    """Read-only HTML report behind the e-mailed "Raporu görüntüle" button (no login needed)."""
    run = get_session().store.load_shared(token)
    if run is None:
        return HTMLResponse(status_code=404, headers={"X-Robots-Tag": "noindex"}, content=(
            "<!doctype html><meta charset='utf-8'><title>Rapor bulunamadı</title>"
            "<body style='font-family:system-ui,sans-serif;max-width:560px;margin:15vh auto;padding:0 16px;color:#0f172a'>"
            "<h1 style='font-size:20px'>Rapor bulunamadı</h1>"
            "<p style='color:#475569'>Bu bağlantının süresi dolmuş ya da bağlantı hatalı. "
            "Raporu gönderen kişiden yeni bir bağlantı isteyin veya e-postanın ekindeki HTML dosyasını açın.</p></body>"))
    return HTMLResponse(reporting.render(run, "html"), headers={"X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer"})


class RagQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


@app.post("/api/rag/ask")
def ask_documents(payload: RagQuestion, user: User = Depends(require_user)):
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="Bir soru yazın.")
    try:
        from core.rag import DocumentIndex, StaleIndexError
        from core.rag_answer import answer_question

        settings = get_settings()
        try:
            store = DocumentIndex(settings.rag_db)
            return answer_question(store, question, top_k=6, top_n=3, model=settings.rag_chat_model)
        except StaleIndexError:
            # Documents were re-indexed from the CLI while the dashboard was running.
            store = DocumentIndex(settings.rag_db, fresh=True)
            return answer_question(store, question, top_k=6, top_n=3, model=settings.rag_chat_model)
    except ImportError as error:
        raise HTTPException(status_code=503, detail="RAG bağımlılıkları kurulu değil.") from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=503,
            detail="Belge yanıtı alınamadı. Ollama'nın çalıştığını kontrol edip tekrar deneyin.",
        ) from error
