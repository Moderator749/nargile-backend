"""Who may call the /integrations routes: a service account (app.models.service_account),
from this machine only.

Two locks, both required:

1. The call is made on this server. uvicorn runs behind Caddy with
   --proxy-headers and trusts only 127.0.0.1, so a request from the internet
   arrives with the visitor's address, and with Caddy's X-Forwarded-For. Only
   a direct call to 127.0.0.1:8000 has a loopback address and no forwarding
   headers. Anything else is told the route does not exist (404), as
   require_superadmin does.
2. A live token with the route's scope, in X-Service-Token: 401 without one,
   403 when the account lacks the scope.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.service_account import ServiceAccount

TOKEN_HEADER = "X-Service-Token"
TOKEN_PREFIX = "nsa_"
LOOPBACK = frozenset({"127.0.0.1", "::1"})
# Headers a proxy adds: their presence means the call did not start on this machine.
FORWARDING_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")
# last_used_at is refreshed at most this often.
USE_RESOLUTION = timedelta(minutes=1)


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_local(request: Request) -> bool:
    host = request.client.host if request.client else None
    return host in LOOPBACK and not any(request.headers.get(h) for h in FORWARDING_HEADERS)


def require_service(scope: str):
    """A route dependency: the calling service account, holding `scope`."""

    async def dependency(
        request: Request,
        token: str | None = Header(default=None, alias=TOKEN_HEADER),
        db: AsyncSession = Depends(get_db),
    ) -> ServiceAccount:
        if not is_local(request):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
        if not token or not token.startswith(TOKEN_PREFIX):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Service token required")
        account = await db.scalar(select(ServiceAccount).where(ServiceAccount.token_hash == token_hash(token)))
        if account is None or account.revoked_at is not None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked service token")
        if scope not in (account.scopes or []):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Missing scope: {scope}")
        now = datetime.now(timezone.utc)
        if account.last_used_at is None or now - account.last_used_at >= USE_RESOLUTION:
            account.last_used_at = now
            await db.commit()
        return account

    return dependency
