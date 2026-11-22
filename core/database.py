"""SQLite database shared by run history, user accounts and login sessions."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    owner_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    started_at     TEXT NOT NULL,
    target_type    TEXT NOT NULL,
    target_url     TEXT NOT NULL,
    total_tests    INTEGER NOT NULL,
    failed_tests   INTEGER NOT NULL,
    worst_severity TEXT,
    data           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_shares (
    token_hash TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    recipients    TEXT NOT NULL,
    interval_days INTEGER NOT NULL,
    send_time     TEXT NOT NULL,
    site          TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    next_run_at   TEXT NOT NULL,
    last_sent_at  TEXT,
    last_error    TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS runs_by_owner ON runs(owner_id, started_at DESC);
"""


class Database:
    """Opens a short-lived connection per operation, so it is safe across threads."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:  # commits on success, rolls back on error
                yield connection
        finally:
            connection.close()
