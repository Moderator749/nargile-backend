from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    now = datetime.now(timezone.utc)
    to_encode = data.copy()
    expire = now + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire, "iat": now})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


#: Roles that work in the admin panel: they get the short session.
PANEL_ROLES = frozenset({"SUPER_ADMIN", "ADMIN", "STAFF"})


def session_lifetime(role: str) -> timedelta:
    """How long a session lasts without a visit: a day for the panel's staff,
    CUSTOMER_SESSION_DAYS for everyone who shops."""
    if str(getattr(role, "value", role)) in PANEL_ROLES:
        return timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return timedelta(days=settings.CUSTOMER_SESSION_DAYS)


def create_session_token(user) -> str:
    """A signed-in session for `user`, stamped with its token version (see User.token_version)."""
    return create_access_token(
        {"sub": str(user.id), "role": str(getattr(user.role, "value", user.role)), "ver": user.token_version or 0},
        expires_delta=session_lifetime(user.role),
    )


def decode_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return None
