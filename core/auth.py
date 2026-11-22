"""
User accounts and login sessions for the dashboard.

Passwords are stored only as salted scrypt hashes. A login creates a random
session token; the browser keeps the token in an HttpOnly cookie and the
database keeps only its SHA-256 hash, so a leaked database cannot be used to
take over a session.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from core.database import Database

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256
SESSION_LIFETIME = timedelta(days=7)

# After this many wrong passwords for one username, wait LOCKOUT_SECONDS.
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 60

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


class AuthError(ValueError):
    """A message that can be shown to the user as is."""


@dataclass(frozen=True)
class User:
    id: int
    username: str


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), dklen=len(digest) // 2,
                                   n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest)


# A real hash to check against when the username does not exist, so a wrong
# username takes as long as a wrong password and does not reveal which users exist.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UserStore:
    def __init__(self, database: Database):
        self.db = database
        self._failures: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    # -- accounts ---------------------------------------------------------
    def count(self) -> int:
        with self.db.connect() as db:
            return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def create(self, username: str, password: str) -> User:
        username = (username or "").strip()
        if not USERNAME_PATTERN.match(username):
            raise AuthError("Kullanıcı adı 3-32 karakter olmalı; yalnızca harf, rakam, nokta, alt çizgi ve tire içerebilir.")
        if len(password or "") < MIN_PASSWORD_LENGTH:
            raise AuthError(f"Şifre en az {MIN_PASSWORD_LENGTH} karakter olmalı.")
        if len(password) > MAX_PASSWORD_LENGTH:
            raise AuthError(f"Şifre en fazla {MAX_PASSWORD_LENGTH} karakter olabilir.")
        try:
            with self.db.connect() as db:
                cursor = db.execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                    (username, hash_password(password), _now().isoformat()),
                )
        except sqlite3.IntegrityError as error:
            raise AuthError("Bu kullanıcı adı zaten kullanılıyor.") from error
        return User(id=cursor.lastrowid, username=username)

    def authenticate(self, username: str, password: str) -> User:
        key = (username or "").strip().lower()
        self._check_lockout(key)
        with self.db.connect() as db:
            row = db.execute("SELECT id, username, password_hash FROM users WHERE username = ?",
                             ((username or "").strip(),)).fetchone()
        ok = verify_password(password or "", row["password_hash"] if row else _DUMMY_HASH)
        if not (row and ok):
            self._record_failure(key)
            raise AuthError("Kullanıcı adı veya şifre hatalı.")
        with self._lock:
            self._failures.pop(key, None)
        return User(id=row["id"], username=row["username"])

    def _check_lockout(self, key: str) -> None:
        with self._lock:
            recent = [t for t in self._failures.get(key, []) if time.monotonic() - t < LOCKOUT_SECONDS]
            self._failures[key] = recent
            if len(recent) >= MAX_FAILED_LOGINS:
                wait = int(LOCKOUT_SECONDS - (time.monotonic() - recent[0])) + 1
                raise AuthError(f"Çok fazla hatalı deneme. {wait} saniye sonra tekrar deneyin.")

    def _record_failure(self, key: str) -> None:
        with self._lock:
            self._failures.setdefault(key, []).append(time.monotonic())

    # -- sessions ---------------------------------------------------------
    def create_session(self, user: User) -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        with self.db.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))
            db.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (_token_hash(token), user.id, now.isoformat(), (now + SESSION_LIFETIME).isoformat()),
            )
        return token

    def user_for_session(self, token: Optional[str]) -> Optional[User]:
        if not token:
            return None
        with self.db.connect() as db:
            row = db.execute(
                """SELECT users.id, users.username FROM sessions JOIN users ON users.id = sessions.user_id
                   WHERE sessions.token_hash = ? AND sessions.expires_at > ?""",
                (_token_hash(token), _now().isoformat()),
            ).fetchone()
        return User(id=row["id"], username=row["username"]) if row else None

    def end_session(self, token: Optional[str]) -> None:
        if token:
            with self.db.connect() as db:
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
