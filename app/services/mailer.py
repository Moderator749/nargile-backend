"""Transactional e-mail: rendering and delivery.

Delivery is best-effort and never breaks the operation that triggered it — an
admin marking an order shipped must succeed even if the mail server is down.
With no SMTP_HOST configured the rendered message is logged at INFO instead of
being sent, so the notice can be read off the console in development.
"""

import asyncio
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    text: str
    html: str


def _build(mail: Mail) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = mail.subject
    msg["From"] = formataddr((settings.MAIL_FROM_NAME, settings.MAIL_FROM))
    msg["To"] = mail.to
    msg.set_content(mail.text)
    msg.add_alternative(mail.html, subtype="html")
    return msg


def _send_blocking(mail: Mail) -> None:
    """Hand the message to the SMTP server. Runs in a worker thread."""
    msg = _build(mail)
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT) as smtp:
        if settings.SMTP_STARTTLS:
            smtp.starttls()
        if settings.SMTP_USER:
            smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD or "")
        smtp.send_message(msg)


async def send(mail: Mail) -> bool:
    """Send (or log) one message. Returns whether it left the building."""
    if not settings.SMTP_HOST:
        logger.info(
            "E-MAIL (SMTP yapılandırılmadı, gönderilmedi)\nTo: %s\nSubject: %s\n\n%s",
            mail.to,
            mail.subject,
            mail.text,
        )
        return False
    try:
        # smtplib is blocking; keep it off the event loop.
        await asyncio.to_thread(_send_blocking, mail)
        logger.info("E-mail sent to %s: %s", mail.to, mail.subject)
        return True
    except Exception:
        logger.exception("E-mail to %s failed: %s", mail.to, mail.subject)
        return False
