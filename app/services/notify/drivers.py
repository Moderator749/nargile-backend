"""The drivers: one small class per provider, all answering the same way.

Each `send` returns a `Delivery` and never raises: a provider that is down or
refuses the message is a failed delivery, logged with its reason, not an error
for whatever triggered it. The gateways' request shapes follow their public
HTTP APIs (Netgsm REST v2, İleti Merkezi JSON v1, Resend); check them against
the providers' current documentation before going live.
"""

import asyncio
import logging
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.services import mailer

logger = logging.getLogger("notify")


@dataclass(frozen=True)
class Delivery:
    ok: bool
    provider: str
    ref: str | None = None
    error: str | None = None


# ── E-mail ───────────────────────────────────────────────────────────────────


class MockEmail:
    name = "mock"

    async def send(self, to: str, subject: str, text: str, html: str) -> Delivery:
        logger.info("E-MAIL [mock, gönderilmedi] To: %s | Subject: %s\n%s", to, subject, text)
        return Delivery(True, self.name)


class SmtpEmail:
    name = "smtp"

    async def send(self, to: str, subject: str, text: str, html: str) -> Delivery:
        try:
            await asyncio.to_thread(mailer._send_blocking, mailer.Mail(to=to, subject=subject, text=text, html=html))
            return Delivery(True, self.name)
        except Exception as exc:  # noqa: BLE001 — any SMTP failure is a failed delivery
            logger.warning("SMTP to %s failed: %s", to, exc)
            return Delivery(False, self.name, error=f"{type(exc).__name__}: {exc}"[:500])


class ResendEmail:
    name = "resend"

    async def send(self, to: str, subject: str, text: str, html: str) -> Delivery:
        try:
            async with httpx.AsyncClient(timeout=settings.SMTP_TIMEOUT) as client:
                resp = await client.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
                    json={
                        "from": f"{settings.MAIL_FROM_NAME} <{settings.MAIL_FROM}>",
                        "to": [to],
                        "subject": subject,
                        "text": text,
                        "html": html,
                    },
                )
            if resp.status_code >= 300:
                return Delivery(False, self.name, error=f"HTTP {resp.status_code}: {resp.text[:300]}")
            return Delivery(True, self.name, ref=str(resp.json().get("id") or "") or None)
        except httpx.HTTPError as exc:
            return Delivery(False, self.name, error=f"{type(exc).__name__}: {exc}"[:500])


def email_driver():
    """The configured e-mail driver; the mock when its credentials are missing."""
    choice = (settings.EMAIL_DRIVER or "auto").lower()
    if choice == "auto":
        choice = "smtp" if settings.SMTP_HOST else "mock"
    if choice == "smtp" and settings.SMTP_HOST:
        return SmtpEmail()
    if choice == "resend" and settings.RESEND_API_KEY:
        return ResendEmail()
    return MockEmail()


# ── SMS ──────────────────────────────────────────────────────────────────────
# `numbers` are normalised Turkish mobiles (905XXXXXXXXX); one call may carry
# many, and the result stands for all of them.


class MockSms:
    name = "mock"

    async def send(self, numbers: list[str], text: str) -> Delivery:
        logger.info("SMS [mock, gönderilmedi] To: %s\n%s", ", ".join(numbers), text)
        return Delivery(True, self.name)


async def _post(url: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(timeout=settings.SMS_TIMEOUT) as client:
        return await client.post(url, **kwargs)


NETGSM_URL = "https://api.netgsm.com.tr/sms/rest/v2/send"


@dataclass(frozen=True)
class NetgsmAccount:
    usercode: str
    password: str
    header: str

    @property
    def complete(self) -> bool:
        return bool(self.usercode and self.password and self.header)


def env_netgsm() -> NetgsmAccount:
    """The Netgsm account in the server's .env."""
    return NetgsmAccount(
        usercode=(settings.NETGSM_USERCODE or "").strip(),
        password=settings.NETGSM_PASSWORD or "",
        header=(settings.NETGSM_HEADER or settings.SMS_SENDER or "").strip(),
    )


class NetgsmSms:
    """Netgsm's REST v2 API: HTTP basic auth with the account, JSON body."""

    name = "netgsm"

    def __init__(self, account: NetgsmAccount):
        self.account = account

    async def send(self, numbers: list[str], text: str) -> Delivery:
        try:
            resp = await _post(
                NETGSM_URL,
                auth=(self.account.usercode, self.account.password),
                json={
                    "msgheader": self.account.header,
                    "encoding": "TR",
                    "messages": [{"msg": text, "no": n[2:]} for n in numbers],
                },
            )
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code < 300 and str(data.get("code")) == "00":
                return Delivery(True, self.name, ref=str(data.get("jobid") or "") or None)
            return Delivery(False, self.name, error=f"HTTP {resp.status_code}: {resp.text[:300]}")
        except (httpx.HTTPError, ValueError) as exc:
            return Delivery(False, self.name, error=f"{type(exc).__name__}: {exc}"[:500])


class IletimerkeziSms:
    name = "iletimerkezi"

    async def send(self, numbers: list[str], text: str) -> Delivery:
        body = {
            "request": {
                "authentication": {"key": settings.ILETIMERKEZI_API_KEY, "hash": settings.ILETIMERKEZI_API_HASH},
                "order": {
                    "sender": settings.SMS_SENDER,
                    "sendDateTime": [],
                    # (sic) — the API spells it "receipents".
                    "message": {"text": text, "receipents": {"number": numbers}},
                },
            }
        }
        try:
            resp = await _post("https://api.iletimerkezi.com/v1/send-sms/json", json=body)
            status = (resp.json().get("response") or {}).get("status") or {}
            if str(status.get("code")) == "200":
                order = (resp.json().get("response") or {}).get("order") or {}
                return Delivery(True, self.name, ref=str(order.get("id") or "") or None)
            return Delivery(False, self.name, error=f"{status.get('code')}: {status.get('message') or resp.text[:300]}")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            return Delivery(False, self.name, error=f"{type(exc).__name__}: {exc}"[:500])


class WebhookSms:
    """Any gateway behind a small relay: POST {to, message, sender}."""

    name = "webhook"

    async def send(self, numbers: list[str], text: str) -> Delivery:
        headers = {"Authorization": f"Bearer {settings.SMS_WEBHOOK_TOKEN}"} if settings.SMS_WEBHOOK_TOKEN else {}
        try:
            resp = await _post(
                settings.SMS_WEBHOOK_URL or "",
                headers=headers,
                json={"to": numbers, "message": text, "sender": settings.SMS_SENDER},
            )
            if resp.status_code >= 300:
                return Delivery(False, self.name, error=f"HTTP {resp.status_code}: {resp.text[:300]}")
            return Delivery(True, self.name)
        except httpx.HTTPError as exc:
            return Delivery(False, self.name, error=f"{type(exc).__name__}: {exc}"[:500])


def sms_driver(netgsm: NetgsmAccount | None = None, *, netgsm_chosen: bool = False):
    """The configured SMS driver; the mock when its credentials are missing.

    `netgsm` is the account to send with (the panel's over the .env's, see
    app.services.sms_settings); `netgsm_chosen` says the panel holds one, which
    picks Netgsm whatever SMS_DRIVER says.
    """
    choice = (settings.SMS_DRIVER or "mock").lower()
    account = netgsm or env_netgsm()
    if (netgsm_chosen or choice == "netgsm") and account.complete:
        return NetgsmSms(account)
    if choice == "iletimerkezi" and settings.ILETIMERKEZI_API_KEY and settings.ILETIMERKEZI_API_HASH and settings.SMS_SENDER:
        return IletimerkeziSms()
    if choice == "webhook" and settings.SMS_WEBHOOK_URL:
        return WebhookSms()
    return MockSms()
