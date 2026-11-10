""""Abonelikten Çık": leaving campaign e-mail in one click, without logging in.

Every campaign e-mail carries a link of its own (the footer) and the
List-Unsubscribe pair of headers (RFC 8058), which put an "Unsubscribe"
button in Gmail, Outlook, Yahoo… — required of bulk senders, and the opt-out
the ETK rules ask for in each commercial message.

The token is the address and a signature over it (HMAC with SECRET_KEY): no
table of tokens, and one that still works years later, as an unsubscribe link
must. It names the address to the one who already holds the e-mail, and
nothing more can be done with it than unsubscribing that address.
"""

import base64
import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.notification import EmailOptOut
from app.models.user import User

#: The page that confirms it, on the storefront.
PAGE_PATH = "/abonelikten-cik"
#: The API route, through the storefront's proxy (the mail app POSTs here).
ONE_CLICK_PATH = "/api/backend/api/v1/newsletter/unsubscribe"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(email: str) -> str:
    digest = hmac.new(settings.SECRET_KEY.encode(), f"unsubscribe:{email}".encode(), hashlib.sha256).digest()
    return _b64(digest[:18])


def token_for(email: str) -> str:
    email = email.strip().lower()
    return f"{_b64(email.encode())}.{_signature(email)}"


def email_from(token: str) -> str | None:
    """The address a token was made for, or None when it is not one of ours."""
    try:
        payload, signature = token.split(".", 1)
        email = _unb64(payload).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if "@" not in email or not hmac.compare_digest(signature, _signature(email)):
        return None
    return email


def page_url(email: str) -> str:
    """The footer's "Abonelikten Çık": a page that unsubscribes and says so."""
    return f"{settings.UNSUBSCRIBE_BASE_URL.rstrip('/')}{PAGE_PATH}?token={token_for(email)}"


def one_click_url(email: str) -> str:
    return f"{settings.UNSUBSCRIBE_BASE_URL.rstrip('/')}{ONE_CLICK_PATH}?token={token_for(email)}"


def headers(email: str) -> dict[str, str]:
    """List-Unsubscribe (the one-click URL, and a reply to the shop for mail
    apps that only write) and List-Unsubscribe-Post, which marks the URL as
    one-click: the mail app POSTs to it and that is all."""
    mailto = f"mailto:{settings.MAIL_FROM}?subject={quote('Abonelikten çık')}"
    return {
        "List-Unsubscribe": f"<{one_click_url(email)}>, <{mailto}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


async def opt_out(db: AsyncSession, email: str, source: str) -> None:
    """No more campaign e-mail to this address: the consent of every account
    under it cleared (the date it was given goes with it, as a withdrawal in
    "Hesabım" does), and the address noted for typed-in lists. Doing it again
    changes nothing. Committed."""
    email = email.strip().lower()
    await db.execute(
        update(User)
        .where(func.lower(User.email) == email, User.marketing_email_consent_at.is_not(None))
        .values(marketing_email_consent_at=None)
    )
    await db.execute(
        insert(EmailOptOut)
        .values(email=email, source=source, created_at=datetime.now(timezone.utc))
        .on_conflict_do_nothing(index_elements=[EmailOptOut.email])
    )
    await db.commit()
