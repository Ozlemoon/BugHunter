"""
E-mail a run's results as a short report.

The message shows the tested system's name, the number of tests and how many
passed and failed, with a "Raporu görüntüle" button that opens the HTML report
through a read-only share link. The same HTML report is attached, so it opens
even when the recipient cannot reach the dashboard's address.
"""

from __future__ import annotations

import re
import smtplib
import ssl
from datetime import datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

from core import reporting
from core.config import Settings

if TYPE_CHECKING:
    from core.engine import HuntRun

LOGO = Path(__file__).resolve().parent / "templates" / "assets" / "logo-email.png"
MAX_RECIPIENTS = 10
MAX_NOTE_LENGTH = 1000
# Deliberately strict: one plain address, no display names, no line breaks (header injection).
EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                           r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


NOT_CONFIGURED = ("E-posta gönderimi ayarlanmamış. .env dosyasına BUGHUNTER_SMTP_HOST ve BUGHUNTER_SMTP_FROM "
                  "(gerekirse BUGHUNTER_SMTP_USER ve BUGHUNTER_SMTP_PASSWORD) yazıp paneli yeniden başlatın.")


LOGIN_REFUSED = ("E-posta sunucusu girişi kabul etmedi. Gmail kullanıyorsanız BUGHUNTER_SMTP_PASSWORD satırına "
                 "hesap şifrenizi değil, Google hesabı → Güvenlik → Uygulama şifreleri bölümünden alınan 16 harfli "
                 "uygulama şifresini yazın ve paneli yeniden başlatın.")


class MailError(RuntimeError):
    """A message that can be shown to the user as is."""


def parse_recipients(raw: Iterable[str] | str) -> List[str]:
    """Splits on commas, semicolons and whitespace; validates and de-duplicates."""
    if isinstance(raw, str):
        raw = [raw]
    found: List[str] = []
    for chunk in raw:
        for address in re.split(r"[,;\s]+", chunk or ""):
            if not address:
                continue
            if not EMAIL_PATTERN.match(address) or len(address) > 254:
                raise MailError(f"Geçersiz e-posta adresi: {address[:80]}")
            if address.lower() not in (a.lower() for a in found):
                found.append(address)
    if not found:
        raise MailError("En az bir e-posta adresi yazın.")
    if len(found) > MAX_RECIPIENTS:
        raise MailError(f"Bir seferde en fazla {MAX_RECIPIENTS} adrese gönderilebilir.")
    return found


def system_name(url: str) -> str:
    """The tested system's name as shown to people: the host, e.g. petstore3.swagger.io."""
    return urlsplit(url).netloc or url


def build_message(run: HuntRun, recipients: List[str], sender: str, report_url: str,
                  sent_by: str, note: Optional[str] = None, screenshots: Optional[dict] = None,
                  pdf: Optional[bytes] = None, comparison: Optional[dict] = None) -> EmailMessage:
    """With `pdf`, the report is attached as PDF; otherwise as the self-contained HTML file.
    `comparison` (core.comparison.compare) adds a "since the previous test" line."""
    name = system_name(run.config.target_url)
    summary = run.summary
    worst = run.worst_severity()
    logo_cid = make_msgid(domain="bughunter.local")
    attachment = f"bughunter-{run.run_id}.{'pdf' if pdf else 'html'}"
    context = dict(run=run, name=name, summary=summary, report_url=report_url, sent_by=sent_by, attachment=attachment,
                   note=(note or "").strip()[:MAX_NOTE_LENGTH], worst=worst.value if worst else None,
                   logo_src=f"cid:{logo_cid[1:-1]}",
                   change=comparison if comparison and comparison.get("previous") else None)

    message = EmailMessage()
    message["Subject"] = (f"BugHunter test raporu: {name} · "
                          f"{summary.passed_tests}/{summary.total_tests} başarılı")
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=_domain(sender))
    message.set_content(reporting.environment().get_template("email.txt").render(**context))
    message.add_alternative(reporting.environment().get_template("email.html").render(**context), subtype="html")
    message.get_payload()[1].add_related(LOGO.read_bytes(), maintype="image", subtype="png", cid=logo_cid,
                                         filename="bughunter.png", disposition="inline")
    if pdf:
        message.add_attachment(pdf, maintype="application", subtype="pdf", filename=attachment)
    else:
        message.add_attachment(reporting.render(run, "html", screenshots, comparison).encode("utf-8"), maintype="text",
                               subtype="html", filename=attachment)
    return message


SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW")


def digest_context(runs: List[HuntRun], since: datetime, until: datetime, interval_days: int,
                   site: Optional[str], report_links: Dict[str, str]) -> dict:
    """Groups the period's runs by site: counts, severities and the most important bugs."""
    sites: Dict[str, dict] = {}
    for run in runs:  # newest first
        name = system_name(run.config.target_url)
        entry = sites.setdefault(name, {"name": name, "runs": 0, "total": 0, "passed": 0, "failed": 0,
                                        "severity": {k: 0 for k in SEVERITY_ORDER}, "bugs": {},
                                        "last_run": run.started_at, "link": report_links.get(name)})
        entry["runs"] += 1
        entry["total"] += run.summary.total_tests
        entry["passed"] += run.summary.passed_tests
        entry["failed"] += run.summary.failed_tests
        for finding in run.findings:
            if finding.remediation is not None and not finding.remediation.noteworthy:
                continue  # probable false alarms stay out of the digest
            title = finding.remediation.headline if finding.remediation else finding.evidence.title
            location = (finding.remediation.location if finding.remediation else "") or finding.evidence.endpoint or ""
            # The same failing test in an older run is the same bug (titles may carry timings).
            key = finding.evidence.test_id or title
            if key not in entry["bugs"]:  # runs are newest first, so the latest wording wins
                entry["bugs"][key] = {"title": title, "location": location,
                                      "severity": finding.evidence.severity.value}
    for entry in sites.values():
        for bug in entry["bugs"].values():
            entry["severity"][bug["severity"]] = entry["severity"].get(bug["severity"], 0) + 1
        bugs = sorted(entry["bugs"].values(), key=lambda b: SEVERITY_ORDER.index(b["severity"])
                      if b["severity"] in SEVERITY_ORDER else 9)
        entry["bug_count"] = len(bugs)
        entry["top_bugs"] = bugs[:5]
        entry["more_bugs"] = max(0, len(bugs) - 5)
    ordered = sorted(sites.values(), key=lambda e: (-e["severity"]["CRITICAL"], -e["severity"]["HIGH"], -e["failed"]))
    return {
        "sites": ordered,
        "since": since.astimezone(), "until": until.astimezone(), "interval_days": interval_days, "site": site,
        "totals": {"runs": len(runs), "tests": sum(e["total"] for e in ordered),
                   "passed": sum(e["passed"] for e in ordered), "failed": sum(e["failed"] for e in ordered),
                   "bugs": sum(e["bug_count"] for e in ordered),
                   "critical": sum(e["severity"]["CRITICAL"] + e["severity"]["HIGH"] for e in ordered)},
    }


def build_digest(runs: List[HuntRun], recipients: List[str], sender: str, since: datetime, until: datetime,
                 interval_days: int, site: Optional[str], report_links: Dict[str, str], sent_by: str) -> EmailMessage:
    context = digest_context(runs, since, until, interval_days, site, report_links)
    totals = context["totals"]
    period = "son 24 saat" if interval_days == 1 else f"son {interval_days} gün"
    logo_cid = make_msgid(domain="bughunter.local")
    context.update(period=period, sent_by=sent_by, logo_src=f"cid:{logo_cid[1:-1]}")

    message = EmailMessage()
    scope = f"{site} · " if site else ""
    message["Subject"] = (f"BugHunter özet raporu: {scope}{period} · {totals['tests']} test · "
                          f"{totals['bugs']} hata" if runs else f"BugHunter özet raporu: {scope}{period} · yeni test yok")
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=_domain(sender))
    env = reporting.environment()
    message.set_content(env.get_template("digest.txt").render(**context))
    message.add_alternative(env.get_template("digest.html").render(**context), subtype="html")
    message.get_payload()[1].add_related(LOGO.read_bytes(), maintype="image", subtype="png", cid=logo_cid,
                                         filename="bughunter.png", disposition="inline")
    return message


def _domain(sender: str) -> str:
    try:
        return Address(addr_spec=sender).domain or "bughunter.local"
    except (ValueError, IndexError):
        return "bughunter.local"


def send(message: EmailMessage, settings: Settings) -> None:
    if not settings.mail_configured:
        raise MailError(NOT_CONFIGURED)
    timeout = 20
    logging_in = False
    try:
        if settings.smtp_security == "ssl":
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
        with server:
            if settings.smtp_security == "starttls":
                server.starttls(context=ssl.create_default_context())
            if settings.smtp_user:
                logging_in = True
                server.login(settings.smtp_user, settings.smtp_password)
                logging_in = False
            server.send_message(message)
    except smtplib.SMTPAuthenticationError as error:
        raise MailError(LOGIN_REFUSED) from error
    except smtplib.SMTPServerDisconnected as error:
        if logging_in:  # Gmail hangs up instead of answering when given an account password
            raise MailError(LOGIN_REFUSED) from error
        raise MailError(f"E-posta sunucusu bağlantıyı kapattı ({settings.smtp_host}:{settings.smtp_port}).") from error
    except smtplib.SMTPRecipientsRefused as error:
        refused = ", ".join(error.recipients)
        raise MailError(f"E-posta sunucusu şu adresleri reddetti: {refused}") from error
    except (smtplib.SMTPException, OSError) as error:
        raise MailError(f"E-posta gönderilemedi ({settings.smtp_host}:{settings.smtp_port}): {error}") from error
