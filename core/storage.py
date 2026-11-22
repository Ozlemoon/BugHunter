"""Run history stored in the SQLite database (reports/bughunter.db)."""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from core.database import Database

if TYPE_CHECKING:
    from core.engine import HuntRun

logger = logging.getLogger(__name__)

RUN_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class RunStore:
    """
    One row per hunt. `owner_id` is the dashboard user who started it; runs
    started from the CLI have no owner. JSON files from the older file-based
    history (reports/runs/*.json) are imported once, without an owner.
    """

    def __init__(self, database: Database | Path, legacy_dir: Optional[Path] = None):
        self.db = database if isinstance(database, Database) else Database(database)
        if legacy_dir is not None:
            self._import_legacy(Path(legacy_dir))

    # ------------------------------------------------------------------
    def save(self, run: HuntRun, owner_id: Optional[int] = None) -> None:
        """Insert or update. An existing owner is kept when `owner_id` is not given."""
        from core.redaction import redact_run

        if not RUN_ID_PATTERN.match(run.run_id):
            raise ValueError("Invalid run id.")
        run = redact_run(run)
        worst = run.worst_severity()
        with self.db.connect() as db:
            db.execute(
                """INSERT INTO runs (run_id, owner_id, started_at, target_type, target_url,
                                     total_tests, failed_tests, worst_severity, data)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                       owner_id = COALESCE(excluded.owner_id, runs.owner_id),
                       total_tests = excluded.total_tests, failed_tests = excluded.failed_tests,
                       worst_severity = excluded.worst_severity, data = excluded.data""",
                (run.run_id, owner_id, run.started_at.isoformat(), run.config.target_type.value,
                 run.config.target_url, run.summary.total_tests, run.summary.failed_tests,
                 worst.value if worst else None, run.model_dump_json()),
            )

    def load(self, run_id: str, owner_id: Optional[int] = None) -> Optional[HuntRun]:
        """`owner_id` given → only that user's run is returned."""
        from core.engine import HuntRun

        if not RUN_ID_PATTERN.match(run_id or ""):
            return None
        query, params = "SELECT data FROM runs WHERE run_id = ?", [run_id]
        if owner_id is not None:
            query += " AND owner_id = ?"
            params.append(owner_id)
        with self.db.connect() as db:
            row = db.execute(query, params).fetchone()
        return HuntRun.model_validate_json(row["data"]) if row else None

    def owner_of(self, run_id: str) -> Optional[int]:
        with self.db.connect() as db:
            row = db.execute("SELECT owner_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return row["owner_id"] if row else None

    def list(self, limit: int = 20, owner_id: Optional[int] = None) -> List[HuntRun]:
        """Newest first. `owner_id` given → only that user's runs; otherwise all runs."""
        from core.engine import HuntRun

        query, params = "SELECT run_id, data FROM runs", []
        if owner_id is not None:
            query += " WHERE owner_id = ?"
            params.append(owner_id)
        query += " ORDER BY started_at DESC, run_id DESC LIMIT ?"
        params.append(limit)
        with self.db.connect() as db:
            rows = db.execute(query, params).fetchall()
        runs = []
        for row in rows:
            try:
                runs.append(HuntRun.model_validate_json(row["data"]))
            except ValueError as error:  # skip rows from an incompatible older version
                logger.warning("Skipping unreadable run %s: %s", row["run_id"], error)
        return runs

    def list_since(self, owner_id: int, since: datetime, limit: int = 500) -> List[HuntRun]:
        """The user's runs started at or after `since`, newest first."""
        from core.engine import HuntRun

        with self.db.connect() as db:
            rows = db.execute(
                "SELECT run_id, data FROM runs WHERE owner_id = ? AND started_at >= ? "
                "ORDER BY started_at DESC LIMIT ?",
                (owner_id, since.astimezone(timezone.utc).isoformat(), limit),
            ).fetchall()
        runs = []
        for row in rows:
            try:
                runs.append(HuntRun.model_validate_json(row["data"]))
            except ValueError as error:
                logger.warning("Skipping unreadable run %s: %s", row["run_id"], error)
        return runs

    def assign_unowned(self, owner_id: int) -> int:
        """Gives every run without an owner to `owner_id`; returns how many."""
        with self.db.connect() as db:
            return db.execute("UPDATE runs SET owner_id = ? WHERE owner_id IS NULL", (owner_id,)).rowcount

    # -- read-only links for e-mailed reports ------------------------------
    def create_share(self, run_id: str, days: int = 30) -> str:
        """A random link token that opens this run's HTML report without logging in."""
        token = secrets.token_urlsafe(24)
        now = datetime.now(timezone.utc)
        with self.db.connect() as db:
            db.execute("DELETE FROM report_shares WHERE expires_at < ?", (now.isoformat(),))
            db.execute("INSERT INTO report_shares (token_hash, run_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                       (_hash(token), run_id, now.isoformat(), (now + timedelta(days=days)).isoformat()))
        return token

    def load_shared(self, token: str) -> Optional[HuntRun]:
        if not token or len(token) > 100:
            return None
        with self.db.connect() as db:
            row = db.execute("SELECT run_id FROM report_shares WHERE token_hash = ? AND expires_at > ?",
                             (_hash(token), datetime.now(timezone.utc).isoformat())).fetchone()
        return self.load(row["run_id"]) if row else None

    # ------------------------------------------------------------------
    def _import_legacy(self, directory: Path) -> None:
        from core.engine import HuntRun

        if not directory.is_dir():
            return
        imported = 0
        for path in sorted(directory.glob("*.json")):
            if not RUN_ID_PATTERN.match(path.stem) or self._exists(path.stem):
                continue
            try:
                self.save(HuntRun.model_validate_json(path.read_text(encoding="utf-8")))
                imported += 1
            except ValueError as error:
                logger.warning("Skipping unreadable run file %s: %s", path.name, error)
        if imported:
            logger.info("Imported %d runs from %s into the database", imported, directory)

    def _exists(self, run_id: str) -> bool:
        with self.db.connect() as db:
            return db.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is not None
