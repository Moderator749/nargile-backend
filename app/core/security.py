import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


#: The shortest password a customer may choose (registration, "Şifremi
#: unuttum"). An admin account, which can change prices and orders, keeps the
#: stronger minimum whenever its password is set.
CUSTOMER_PASSWORD_MIN = 6
ADMIN_PASSWORD_MIN = 8


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


#: An account brought over from the old OpenCart store keeps that store's hash,
#: stored as "opencart$<salt>$<hex>", until its first login replaces it with
#: bcrypt (see needs_rehash), so its customer signs in with the old password.
OPENCART_PREFIX = "opencart$"


def opencart_hash(password: str | None, salt: str | None) -> str | None:
    """The stored form of an OpenCart customer's password, or None when the
    dump's value is not a hash OpenCart writes (sha1 hex, or md5 hex from 1.x)."""
    hexed = (password or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{32}", hexed):
        return None
    return f"{OPENCART_PREFIX}{salt or ''}${hexed}"


def _php_htmlspecialchars(text: str, quotes: bool) -> str:
    text = text.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    return text.replace("'", "&#039;") if quotes else text


def _verify_opencart(plain: str, stored: str) -> bool:
    salt, _, expected = stored.removeprefix(OPENCART_PREFIX).rpartition("$")
    # OpenCart hashed what its request object handed over: the typed password
    # trimmed and passed through htmlspecialchars (ENT_COMPAT at registration,
    # ENT_QUOTES at login, depending on the version), so each spelling is tried.
    candidates = {
        form
        for text in (plain, plain.strip())
        for form in (text, _php_htmlspecialchars(text, quotes=False), _php_htmlspecialchars(text, quotes=True))
    }
    for form in candidates:
        if len(expected) == 32:
            got = hashlib.md5(form.encode()).hexdigest()
        else:
            sha1 = lambda s: hashlib.sha1(s.encode()).hexdigest()  # noqa: E731
            got = sha1(salt + sha1(salt + sha1(form)))
        if hmac.compare_digest(got, expected):
            return True
    return False


def verify_password(plain: str, hashed: str) -> bool:
    if hashed.startswith(OPENCART_PREFIX):
        return _verify_opencart(plain, hashed)
    return pwd_context.verify(plain, hashed)


def needs_rehash(hashed: str) -> bool:
    """Whether a hash that just verified should be replaced by a fresh bcrypt one."""
    return hashed.startswith(OPENCART_PREFIX) or pwd_context.needs_update(hashed)


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
