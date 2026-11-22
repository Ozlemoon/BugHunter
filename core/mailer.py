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
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, List, Optional
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
                  sent_by: str, note: Optional[str] = None) -> EmailMessage:
    name = system_name(run.config.target_url)
    summary = run.summary
    worst = run.worst_severity()
    logo_cid = make_msgid(domain="bughunter.local")
    context = dict(run=run, name=name, summary=summary, report_url=report_url, sent_by=sent_by,
                   note=(note or "").strip()[:MAX_NOTE_LENGTH], worst=worst.value if worst else None,
                   logo_src=f"cid:{logo_cid[1:-1]}")

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
    message.add_attachment(reporting.render(run, "html").encode("utf-8"), maintype="text", subtype="html",
                           filename=f"bughunter-{run.run_id}.html")
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
