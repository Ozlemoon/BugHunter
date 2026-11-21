"""
Hunt orchestration: the single test → evidence → approval → patch → verify
pipeline shared by the web dashboard, the interactive CLI and headless CI scans.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from pydantic import BaseModel, Field, field_validator

from core.analyzer import BugAnalyzer
from core.config import PROJECT_ROOT, Settings, get_settings
from core.generator import ALL_CRITERIA, TestGenerator
from core.models import (
    BugEvidence,
    FixProposal,
    FixStatus,
    RegressionReport,
    Severity,
    TargetType,
    TestCase,
    TestCriterion,
    TestResult,
)
from core.patcher import CodePatcher, HumanApprovalGate
from core.runner import TestRunner
from core.storage import RunStore
from core.targets.api_target import ApiTargetParser
from core.targets.web_target import WebTargetParser
from core.verifier import RegressionVerifier

logger = logging.getLogger(__name__)

ClientFactory = Callable[[], httpx.Client]


def normalise_target_url(value: str) -> str:
    """
    Accepts what people actually type: 'example.com/docs' becomes 'https://example.com/docs'.
    Local OpenAPI files (existing paths) are left untouched.
    """
    from urllib.parse import urlparse

    value = value.strip()
    if not value:
        raise ValueError("Bir hedef adresi girin.")
    if "://" not in value:
        if Path(value).is_file():
            return value
        # 'javascript:…', 'mailto:…' are schemes; 'localhost:8000' is host:port.
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:(?!\d)", value):
            raise ValueError("Adres http:// veya https:// ile başlamalı.")
        value = f"https://{value.lstrip('/')}"
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Adres http:// veya https:// ile başlamalı.")
    try:
        parsed.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError as error:
        raise ValueError(f"Geçersiz adres: {value}") from error
    if not parsed.hostname or " " in parsed.netloc:
        raise ValueError(f"Geçersiz adres: {value}")
    return value


def describe_connection_error(error: httpx.RequestError) -> str:
    """Turns transport errors into a reason a non-expert can act on."""
    url = str(error.request.url) if getattr(error, "_request", None) is not None else "hedef"
    text = str(error).lower()
    if isinstance(error, httpx.TimeoutException):
        reason = "Hedef zamanında yanıt vermedi (zaman aşımı). Site yavaş olabilir; BUGHUNTER_REQUEST_TIMEOUT değerini artırın."
    elif "getaddrinfo" in text or "name or service not known" in text or "nodename nor servname" in text:
        reason = "Alan adı bulunamadı. Adresi yazım hatasına karşı kontrol edin."
    elif "certificate" in text or "ssl" in text:
        reason = "SSL/TLS sertifikası doğrulanamadı."
    elif "refused" in text or "10061" in text:
        reason = "Bağlantı reddedildi; bu adreste çalışan bir sunucu yok."
    else:
        reason = f"Bağlantı kurulamadı ({type(error).__name__})."
    return f"{url}: {reason}"


class HuntConfig(BaseModel):
    target_url: str = Field(min_length=1, max_length=2048)
    target_type: TargetType = TargetType.API
    criteria: List[TestCriterion] = Field(default_factory=lambda: sorted(ALL_CRITERIA, key=lambda c: c.value))
    sla_threshold_ms: float = Field(default=500.0, gt=0, le=120_000)
    auth_header: Optional[str] = Field(default=None, max_length=4096)  # Authorization header value
    source_file: Optional[str] = Field(default=None, max_length=1024)  # file fixes are proposed against
    crawl_pages: int = Field(default=5, ge=1, le=50)
    same_origin_only: bool = True

    @field_validator("target_url")
    @classmethod
    def _normalise_url(cls, value: str) -> str:
        return normalise_target_url(value)

    @field_validator("auth_header", "source_file")
    @classmethod
    def _blank_is_none(cls, value: Optional[str]) -> Optional[str]:
        value = (value or "").strip()
        return value or None

    @field_validator("criteria")
    @classmethod
    def _non_empty(cls, value: List[TestCriterion]) -> List[TestCriterion]:
        if not value:
            raise ValueError("Select at least one test criterion.")
        return list(dict.fromkeys(value))

    def auth_headers(self) -> Dict[str, str]:
        return {"Authorization": self.auth_header} if self.auth_header else {}


class Finding(BaseModel):
    evidence: BugEvidence
    proposal: Optional[FixProposal] = None
    verification: Optional[RegressionReport] = None


class RunSummary(BaseModel):
    total_tests: int = 0
    passed_tests: int = 0
    failed_tests: int = 0
    fixable_findings: int = 0
    by_severity: Dict[str, int] = Field(default_factory=dict)
    by_criterion: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    duration_ms: float = 0.0


class HuntRun(BaseModel):
    run_id: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    config: HuntConfig
    base_url: str
    tests: List[TestCase] = Field(default_factory=list)
    results: List[TestResult] = Field(default_factory=list)
    findings: List[Finding] = Field(default_factory=list)
    summary: RunSummary = Field(default_factory=RunSummary)

    def test(self, test_id: str) -> Optional[TestCase]:
        return next((t for t in self.tests if t.id == test_id), None)

    def finding_for_fix(self, fix_id: str) -> Optional[Finding]:
        return next((f for f in self.findings if f.proposal and f.proposal.fix_id == fix_id), None)

    def worst_severity(self) -> Optional[Severity]:
        severities = [f.evidence.severity for f in self.findings]
        return max(severities, key=lambda s: s.rank) if severities else None

    def has_findings_at_or_above(self, threshold: Severity) -> bool:
        return any(f.evidence.severity.rank >= threshold.rank for f in self.findings)


def new_run_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def resolve_source_file(source_file: Optional[str], settings: Settings) -> Optional[str]:
    """Validates that a fix target exists and lives under an allowed patch root."""
    if not source_file:
        return None
    path = Path(source_file)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path = path.resolve()
    if not settings.is_patchable(path):
        raise ValueError("Source file is outside the allowed patch roots (BUGHUNTER_PATCH_ROOTS).")
    if not path.is_file():
        raise ValueError(f"Source file not found: {source_file}")
    return str(path)


def playground_source_for(target_url: str, target_type: TargetType, settings: Settings) -> Optional[str]:
    """The bundled demo targets map to their own source files, so demos work without --source."""
    from urllib.parse import urlparse

    parsed = urlparse(target_url)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        return None
    if target_type == TargetType.API and parsed.port == settings.playground_api_port:
        return "playground/sample_api.py"
    if target_type == TargetType.WEB and parsed.port == settings.playground_web_port:
        return "playground/sample_web.py"
    return None


def summarize(run: HuntRun) -> RunSummary:
    by_criterion: Dict[str, Dict[str, int]] = {}
    for test, result in zip(run.tests, run.results):
        bucket = by_criterion.setdefault(test.criterion.value, {"passed": 0, "failed": 0})
        bucket["passed" if result.passed else "failed"] += 1
    passed = sum(1 for r in run.results if r.passed)
    duration = (run.finished_at - run.started_at).total_seconds() * 1000 if run.finished_at else 0.0
    return RunSummary(
        total_tests=len(run.results),
        passed_tests=passed,
        failed_tests=len(run.results) - passed,
        fixable_findings=sum(1 for f in run.findings if f.proposal and f.proposal.patch_available),
        by_severity=dict(Counter(f.evidence.severity.value for f in run.findings)),
        by_criterion=by_criterion,
        duration_ms=round(duration, 1),
    )


class HuntSession:
    """
    Long-lived orchestrator. Thread-safe: the dashboard serves requests from a
    thread pool, so hunts and patch approvals are serialised with a lock.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        store: Optional[RunStore] = None,
        client_factory: Optional[ClientFactory] = None,
        settle_seconds: Optional[float] = None,
    ):
        self.settings = settings or get_settings()
        self.store = store if store is not None else RunStore(self.settings.reports_dir / "runs")
        self.runner = TestRunner(timeout=self.settings.request_timeout)
        self.analyzer = BugAnalyzer()
        self.gate = HumanApprovalGate()
        self.patcher = CodePatcher(approval_gate=self.gate)
        self.verifier = RegressionVerifier()
        self._client_factory = client_factory
        self._settle = self.settings.patch_settle_seconds if settle_seconds is None else settle_seconds
        self._runs: Dict[str, HuntRun] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    def _client(self) -> httpx.Client:
        if self._client_factory:
            return self._client_factory()
        return httpx.Client(timeout=self.settings.request_timeout, follow_redirects=False)

    def plan(self, config: HuntConfig, client: httpx.Client) -> tuple[str, List[TestCase]]:
        criteria = set(config.criteria)
        generator_kwargs = dict(
            criteria=criteria,
            sla_threshold_ms=config.sla_threshold_ms,
            auth_headers=config.auth_headers(),
            same_origin_only=config.same_origin_only,
        )
        if config.target_type == TargetType.API:
            parser = ApiTargetParser(config.target_url, timeout=self.settings.request_timeout,
                                     headers=config.auth_headers(), client=client)
            endpoints = parser.parse()
            base_url = parser.base_url()
            return base_url, TestGenerator(base_url, **generator_kwargs).generate_api_tests(endpoints)

        parser = WebTargetParser(config.target_url, timeout=self.settings.request_timeout,
                                 headers=config.auth_headers(), client=client)
        page = parser.crawl(max_pages=config.crawl_pages)
        return config.target_url, TestGenerator(config.target_url, **generator_kwargs).generate_web_tests(page)

    def start(
        self,
        config: HuntConfig,
        on_result: Optional[Callable[[TestCase, TestResult], None]] = None,
        persist: bool = True,
        on_plan: Optional[Callable[[List[TestCase]], None]] = None,
    ) -> HuntRun:
        """Plans and runs the suite, gathers evidence and drafts (unapproved) fixes."""
        source_file = resolve_source_file(config.source_file, self.settings)
        with self._lock:
            run = HuntRun(run_id=new_run_id(), started_at=datetime.now(timezone.utc), config=config, base_url="")
            with self._client() as client:
                run.base_url, run.tests = self.plan(config, client)
                if on_plan:
                    on_plan(run.tests)
                run.results = self.runner.run_suite(run.tests, client=client, on_result=on_result)

            for test, result in zip(run.tests, run.results):
                if result.passed:
                    continue
                evidence = self.analyzer.analyze_failure(test, result)
                proposal = self.patcher.generate_fix(evidence, source_file) if source_file else None
                run.findings.append(Finding(evidence=evidence, proposal=proposal))
            run.findings.sort(key=lambda f: -f.evidence.severity.rank)

            run.finished_at = datetime.now(timezone.utc)
            run.summary = summarize(run)
            self._runs[run.run_id] = run
            if persist:
                self.store.save(run)
            logger.info("Run %s: %d tests, %d failed", run.run_id, run.summary.total_tests, run.summary.failed_tests)
            return run

    # ------------------------------------------------------------------
    def get_run(self, run_id: str) -> Optional[HuntRun]:
        return self._runs.get(run_id) or self.store.load(run_id)

    def _active_finding(self, run_id: str, fix_id: str) -> tuple[HuntRun, Finding]:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError("Run is not active in this session; start a new run to apply fixes.")
        finding = run.finding_for_fix(fix_id)
        if finding is None or finding.proposal is None:
            raise KeyError(f"Fix proposal '{fix_id}' not found in run {run_id}.")
        return run, finding

    def wait_for_target(self, base_url: str) -> bool:
        """Gives an auto-reloading target time to restart after a patch."""
        if self._settle <= 0:
            return True
        time.sleep(self._settle)
        deadline = time.monotonic() + self.settings.patch_ready_timeout
        while time.monotonic() < deadline:
            try:
                with self._client() as client:
                    client.get(base_url)
                return True
            except httpx.HTTPError:
                time.sleep(0.3)
        return False

    def approve(self, run_id: str, fix_id: str) -> RegressionReport:
        """Human approved: apply the patch, wait for the target, re-run everything."""
        with self._lock:
            run, finding = self._active_finding(run_id, fix_id)
            proposal = finding.proposal
            self.gate.approve_fix(fix_id)
            self.gate.apply_patch(fix_id, create_backup=True)
            if not self.wait_for_target(run.base_url):
                logger.warning("Target %s did not come back after patch %s", run.base_url, fix_id)

            target_test = run.test(finding.evidence.test_id or "")
            if target_test is None:
                raise KeyError(f"Test for {fix_id} not found in run {run_id}.")
            with self._client() as client:
                report = self.verifier.verify(
                    runner=self.runner,
                    client=client,
                    all_tests=run.tests,
                    target_test=target_test,
                    before_results=run.results,
                    proposal=proposal,
                    gate=self.gate,
                    auto_rollback_on_regression=True,
                    auto_rollback_on_failure=True,
                )
            finding.verification = report
            self.store.save(run)
            return report

    def reject(self, run_id: str, fix_id: str) -> FixProposal:
        with self._lock:
            run, finding = self._active_finding(run_id, fix_id)
            self.gate.reject_fix(fix_id)
            self.store.save(run)
            return finding.proposal

    def rollback(self, run_id: str, fix_id: str) -> FixProposal:
        with self._lock:
            run, finding = self._active_finding(run_id, fix_id)
            if finding.proposal.status != FixStatus.APPLIED:
                raise ValueError("Only an applied fix can be rolled back.")
            if not self.gate.rollback(finding.proposal.target_file):
                raise ValueError("No backup found for this file.")
            self.store.save(run)
            return finding.proposal
