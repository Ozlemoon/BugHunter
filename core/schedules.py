"""
Scheduled e-mail digests: "every N days at HH:MM, e-mail me the bugs found".

A schedule belongs to one user. When it is due, the user's runs from the last
N days (optionally for one site only) are compiled into one digest e-mail.
A background thread in the dashboard checks once a minute. If the dashboard
was off when a schedule came due, the digest is sent once when it starts
again; missed periods are not sent one by one.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, List, Optional

from core import mailer
from core.database import Database

if TYPE_CHECKING:
    from core.engine import HuntSession

logger = logging.getLogger(__name__)

TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
MAX_SCHEDULES_PER_USER = 10
MAX_INTERVAL_DAYS = 30
RETRY_AFTER = timedelta(minutes=30)  # after a failed send


class ScheduleError(ValueError):
    """A message that can be shown to the user as is."""


@dataclass
class Schedule:
    id: int
    owner_id: int
    recipients: List[str]
    interval_days: int
    send_time: str              # "HH:MM", local time of the machine running the dashboard
    site: Optional[str]         # host name, or None for every site
    active: bool
    next_run_at: datetime
    last_sent_at: Optional[datetime]
    last_error: Optional[str]
    created_at: datetime

    def as_dict(self) -> dict:
        def local(d):
            return d.astimezone().isoformat() if d else None

        return {"id": self.id, "recipients": self.recipients, "interval_days": self.interval_days,
                "send_time": self.send_time, "site": self.site, "active": self.active,
                "next_run_at": local(self.next_run_at), "last_sent_at": local(self.last_sent_at),
                "last_error": self.last_error}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def first_run(send_time: str, after: datetime) -> datetime:
    """The next moment at `send_time` (local clock) strictly after `after`."""
    hour, minute = (int(x) for x in send_time.split(":"))
    local = after.astimezone()
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def following_run(schedule: Schedule, now: datetime) -> datetime:
    """`interval_days` after the previous due time, skipping periods missed while the dashboard was off."""
    step = timedelta(days=schedule.interval_days)
    upcoming = schedule.next_run_at + step
    while upcoming <= now:
        upcoming += step
    # Keep the chosen wall-clock time across daylight-saving changes.
    hour, minute = (int(x) for x in schedule.send_time.split(":"))
    return upcoming.astimezone().replace(hour=hour, minute=minute, second=0, microsecond=0).astimezone(timezone.utc)


def validate(recipients, interval_days: int, send_time: str) -> List[str]:
    try:
        addresses = mailer.parse_recipients(recipients)
    except mailer.MailError as error:
        raise ScheduleError(str(error)) from error
    if not isinstance(interval_days, int) or not 1 <= interval_days <= MAX_INTERVAL_DAYS:
        raise ScheduleError(f"Sıklık 1 ile {MAX_INTERVAL_DAYS} gün arasında olmalı.")
    if not TIME_PATTERN.match(send_time or ""):
        raise ScheduleError("Saat SS:DD biçiminde olmalı, örneğin 09:00.")
    return addresses


class ScheduleStore:
    def __init__(self, database: Database):
        self.db = database

    @staticmethod
    def _row(row) -> Schedule:
        return Schedule(id=row["id"], owner_id=row["owner_id"],
                        recipients=[a for a in row["recipients"].split(",") if a],
                        interval_days=row["interval_days"], send_time=row["send_time"], site=row["site"],
                        active=bool(row["active"]), next_run_at=_parse(row["next_run_at"]),
                        last_sent_at=_parse(row["last_sent_at"]), last_error=row["last_error"],
                        created_at=_parse(row["created_at"]))

    def create(self, owner_id: int, recipients, interval_days: int, send_time: str,
               site: Optional[str] = None, now: Optional[datetime] = None) -> Schedule:
        addresses = validate(recipients, interval_days, send_time)
        now = now or _now()
        with self.db.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM email_schedules WHERE owner_id = ?", (owner_id,)).fetchone()[0]
            if count >= MAX_SCHEDULES_PER_USER:
                raise ScheduleError(f"En fazla {MAX_SCHEDULES_PER_USER} zamanlanmış rapor oluşturabilirsiniz.")
            cursor = db.execute(
                """INSERT INTO email_schedules (owner_id, recipients, interval_days, send_time, site,
                                                active, next_run_at, created_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                (owner_id, ",".join(addresses), interval_days, send_time, (site or "").strip() or None,
                 first_run(send_time, now).isoformat(), now.isoformat()),
            )
        return self.get(cursor.lastrowid, owner_id)

    def get(self, schedule_id: int, owner_id: Optional[int] = None) -> Optional[Schedule]:
        query, params = "SELECT * FROM email_schedules WHERE id = ?", [schedule_id]
        if owner_id is not None:
            query += " AND owner_id = ?"
            params.append(owner_id)
        with self.db.connect() as db:
            row = db.execute(query, params).fetchone()
        return self._row(row) if row else None

    def list(self, owner_id: int) -> List[Schedule]:
        with self.db.connect() as db:
            rows = db.execute("SELECT * FROM email_schedules WHERE owner_id = ? ORDER BY id", (owner_id,)).fetchall()
        return [self._row(r) for r in rows]

    def due(self, now: datetime) -> List[Schedule]:
        with self.db.connect() as db:
            rows = db.execute("SELECT * FROM email_schedules WHERE active = 1 AND next_run_at <= ? ORDER BY next_run_at",
                              (now.isoformat(),)).fetchall()
        return [self._row(r) for r in rows]

    def set_active(self, schedule_id: int, owner_id: int, active: bool, now: Optional[datetime] = None) -> Schedule:
        schedule = self.get(schedule_id, owner_id)
        if schedule is None:
            raise KeyError(schedule_id)
        next_run = schedule.next_run_at
        if active and next_run <= (now or _now()):  # resuming: don't fire immediately for the paused period
            next_run = first_run(schedule.send_time, now or _now())
        with self.db.connect() as db:
            db.execute("UPDATE email_schedules SET active = ?, next_run_at = ? WHERE id = ?",
                       (int(active), next_run.isoformat(), schedule_id))
        return self.get(schedule_id)

    def delete(self, schedule_id: int, owner_id: int) -> bool:
        with self.db.connect() as db:
            return db.execute("DELETE FROM email_schedules WHERE id = ? AND owner_id = ?",
                              (schedule_id, owner_id)).rowcount == 1

    def claim(self, schedule: Schedule, next_run: datetime) -> bool:
        """Moves a due schedule forward; False when another worker already took it."""
        with self.db.connect() as db:
            return db.execute(
                "UPDATE email_schedules SET next_run_at = ? WHERE id = ? AND next_run_at = ? AND active = 1",
                (next_run.isoformat(), schedule.id, schedule.next_run_at.isoformat()),
            ).rowcount == 1

    def record(self, schedule_id: int, sent_at: Optional[datetime], error: Optional[str],
               retry_at: Optional[datetime] = None) -> None:
        with self.db.connect() as db:
            if error is None:
                db.execute("UPDATE email_schedules SET last_sent_at = ?, last_error = NULL WHERE id = ?",
                           (sent_at.isoformat(), schedule_id))
            else:
                db.execute("UPDATE email_schedules SET last_error = ?, next_run_at = COALESCE(?, next_run_at) WHERE id = ?",
                           (error[:500], retry_at.isoformat() if retry_at else None, schedule_id))


# ---------------------------------------------------------------------------

def send_digest(session: HuntSession, schedule: Schedule, now: Optional[datetime] = None) -> int:
    """Compiles the schedule's period into one e-mail and sends it. Returns the number of runs included."""
    now = now or _now()
    settings = session.settings
    since = now - timedelta(days=schedule.interval_days)
    runs = session.store.list_since(schedule.owner_id, since)
    if schedule.site:
        runs = [r for r in runs if mailer.system_name(r.config.target_url) == schedule.site]
    links = {}
    for name in {mailer.system_name(r.config.target_url) for r in runs}:
        latest = next(r for r in runs if mailer.system_name(r.config.target_url) == name)
        token = session.store.create_share(latest.run_id, days=settings.share_link_days)
        links[name] = f"{settings.base_url}/r/{token}"
    owner = session.users.username(schedule.owner_id) or "BugHunter"
    message = mailer.build_digest(runs, schedule.recipients, settings.smtp_from, since=since, until=now,
                                  interval_days=schedule.interval_days, site=schedule.site,
                                  report_links=links, sent_by=owner)
    mailer.send(message, settings)
    return len(runs)


class Scheduler:
    """Background thread: every `interval` seconds, sends the digests that are due."""

    def __init__(self, session_getter: Callable[[], HuntSession], interval: float = 60.0):
        self._session = session_getter
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="bughunter-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # never let one bad schedule kill the thread
                logger.exception("Scheduled e-mail check failed")
            self._stop.wait(self.interval)

    def tick(self, now: Optional[datetime] = None) -> int:
        """Sends every due digest; returns how many were sent."""
        now = now or _now()
        session = self._session()
        store = ScheduleStore(session.store.db)
        sent = 0
        for schedule in store.due(now):
            if not store.claim(schedule, following_run(schedule, now)):
                continue
            try:
                count = send_digest(session, schedule, now)
                store.record(schedule.id, now, None)
                sent += 1
                logger.info("Scheduled digest %s sent (%d runs) to %d recipient(s)",
                            schedule.id, count, len(schedule.recipients))
            except Exception as error:  # SMTP down, bad credentials ...: try again later
                store.record(schedule.id, None, str(error), retry_at=now + RETRY_AFTER)
                logger.warning("Scheduled digest %s failed: %s", schedule.id, error)
        return sent
