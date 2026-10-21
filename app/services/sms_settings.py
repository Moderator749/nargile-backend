"""SMS Ayarları: the Netgsm account the admin panel holds, and the texts of the
order messages ("Sipariş alındı", "Kargoya verildi").

One `shop_settings` row ("sms") as JSON, so nothing here needs a migration.
The Netgsm password is stored encrypted (Fernet, keyed from SECRET_KEY) and is
never sent back to the panel; a password that no longer decrypts (SECRET_KEY
changed) reads as unset, and the panel asks for it again. Each Netgsm field the
panel leaves blank falls back to the server's .env (see app.core.config).

Like every setting, a row that is missing or hand-edited into nonsense gives
the defaults below instead of an error.
"""

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.notify.drivers import NetgsmAccount, env_netgsm
from app.services.shop_settings import read_setting, save_setting

KEY = "sms"

ORDER_PLACED = "order_placed"
ORDER_SHIPPED = "order_shipped"
EVENTS = (ORDER_PLACED, ORDER_SHIPPED)

#: What each message may say, as {Placeholder}: filled per order.
PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    ORDER_PLACED: ("Name", "OrderNo"),
    ORDER_SHIPPED: ("Name", "OrderNo", "Tracking", "Carrier"),
}

DEFAULT_TEMPLATES: dict[str, str] = {
    ORDER_PLACED: "Sayın {Name}, {OrderNo} numaralı siparişiniz alınmıştır. Bizi tercih ettiğiniz için teşekkür ederiz.",
    ORDER_SHIPPED: "Siparişiniz kargoya verilmiştir. Takip No: {Tracking}",
}

#: Netgsm's sender names (başlık) are at most 11 characters.
HEADER_MAX = 11
TEMPLATE_MAX = 480

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@dataclass
class SmsTemplate:
    enabled: bool
    text: str


@dataclass
class SmsSettings:
    """The panel's values; an empty string is "not set here"."""

    usercode: str = ""
    password: str = ""
    header: str = ""
    templates: dict[str, SmsTemplate] = field(
        default_factory=lambda: {event: SmsTemplate(True, DEFAULT_TEMPLATES[event]) for event in EVENTS}
    )

    @property
    def holds_account(self) -> bool:
        """The panel names a Netgsm account of its own (any field of it)."""
        return bool(self.usercode or self.password or self.header)

    def netgsm(self) -> NetgsmAccount:
        """The account to send with: each field the panel's, else the .env's."""
        env = env_netgsm()
        return NetgsmAccount(
            usercode=self.usercode or env.usercode,
            password=self.password or env.password,
            header=self.header or env.header,
        )


def _fernet() -> Fernet:
    digest = hashlib.sha256(f"sms-settings:{settings.SECRET_KEY}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


def unknown_placeholders(event: str, text: str) -> list[str]:
    """The {Words} in a template that the message cannot fill."""
    allowed = PLACEHOLDERS[event]
    return sorted({name for name in _PLACEHOLDER.findall(text) if name not in allowed})


def render(text: str, values: dict[str, str]) -> str:
    """The template with its placeholders filled; an unknown one is left as written."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text).strip()


async def load(db: AsyncSession) -> SmsSettings:
    raw = await read_setting(db, KEY)
    out = SmsSettings()
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        return out
    out.usercode = str(data.get("usercode") or "").strip()
    out.header = str(data.get("header") or "").strip()
    out.password = _decrypt(str(data["password"])) if data.get("password") else ""
    for event in EVENTS:
        saved = data.get(event)
        if isinstance(saved, dict) and isinstance(saved.get("text"), str) and saved["text"].strip():
            out.templates[event] = SmsTemplate(bool(saved.get("enabled", True)), saved["text"].strip())
    return out


async def save(db: AsyncSession, value: SmsSettings, admin_id=None) -> None:
    """Store the settings, the password encrypted. The caller commits."""
    data = {
        "usercode": value.usercode,
        "header": value.header,
        "password": _fernet().encrypt(value.password.encode()).decode() if value.password else "",
        **{event: {"enabled": t.enabled, "text": t.text} for event, t in value.templates.items()},
    }
    await save_setting(db, KEY, json.dumps(data, ensure_ascii=False), admin_id)
