"""Service accounts: programs on this server that call the API on their own —
the price-intelligence service (radar) first.

A service account is not a user. It signs in with a long random token sent in
X-Service-Token, kept here only as its SHA-256, with the scopes it was given
(`SCOPES`); it reaches only the /integrations routes, and those answer only
calls made on this machine (app.api.service_auth). Accounts are created,
rotated and revoked from the server's shell (scripts/service_accounts.py) —
the token is shown once, at creation or rotation, and never again.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: Read the catalogue the price-intelligence service watches (products, costs, retail prices, sales).
PRICE_READ = "price:read"
#: Every scope an account may hold. Price writes come with their own guarded routes (phase 3).
SCOPES = (PRICE_READ,)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ServiceAccount(Base):
    __tablename__ = "service_accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    # SHA-256 of the token, hex. The token itself is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Its first characters, to tell tokens apart in a listing ("nsa_Xy3k").
    token_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    scopes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Kept up to the minute, not on every call.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set: the token no longer opens anything. Never cleared — create a new account instead.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
