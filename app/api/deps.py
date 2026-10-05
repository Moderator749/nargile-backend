"""FastAPI dependencies for authentication and authorization."""

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.user import User, UserRole

_bearer = HTTPBearer()
_optional_bearer = HTTPBearer(auto_error=False)


async def _user_from_token(token: str, db: AsyncSession) -> User:
    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject",
        )

    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    return user


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    return await _user_from_token(creds.credentials, db)


async def get_optional_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """Anonymous callers get None. A token that *is* sent must be valid (401 otherwise),
    so a client never silently receives anonymous data while believing it is logged in."""
    if creds is None:
        return None
    return await _user_from_token(creds.credentials, db)


async def get_current_admin_user(
    user: User = Depends(get_current_user),
) -> User:
    if user.role not in (UserRole.SUPER_ADMIN, UserRole.ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return user
