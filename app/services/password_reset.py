""""Şifremi unuttum": issue and redeem single-use reset secrets.

By e-mail the secret is a 32-byte random token in a link; by SMS a six-digit
code. Only a SHA-256 of it is stored — of the code with the user's id and the
app's secret, since six digits alone would collide and could be tabled. A
secret lives PASSWORD_RESET_TTL_MINUTES, a new request voids the older ones,
and a code survives PASSWORD_RESET_MAX_ATTEMPTS wrong tries.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import ADMIN_PASSWORD_MIN, hash_password
from app.models.notification import EMAIL, SMS, PasswordResetToken
from app.models.user import User, UserRole
from app.services.notify import dispatch
from app.services.notify.phone import normalize_tr_mobile

PURPOSE = "password_reset"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_link_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_sms_code(user_id: uuid.UUID, code: str) -> str:
    return hashlib.sha256(f"{user_id}:{code}:{settings.SECRET_KEY}".encode()).hexdigest()


async def find_by_email(db: AsyncSession, identifier: str) -> User | None:
    """By e-mail address or username, as the login form takes them."""
    given = identifier.strip().lower()
    return await db.scalar(
        select(User)
        .where((func.lower(User.email) == given) | (func.lower(User.username) == given), User.is_guest.is_(False))
        .order_by(User.created_at)
        .limit(1)
    )


async def find_by_phone(db: AsyncSession, raw: str) -> User | None:
    """The one account with this mobile number; None when there is none or
    more than one (a code must not reset an account nobody can tell apart)."""
    number = normalize_tr_mobile(raw)
    if number is None:
        return None
    last10 = func.right(func.regexp_replace(User.phone, r"\D", "", "g"), 10)
    # A guest checkout is not an account to reset, nor one to tell apart from.
    found = (await db.execute(select(User).where(last10 == number[2:], User.is_guest.is_(False)).limit(2))).scalars().all()
    return found[0] if len(found) == 1 else None


async def _recent_requests(db: AsyncSession, user: User) -> int:
    since = _now() - timedelta(minutes=settings.PASSWORD_RESET_WINDOW_MINUTES)
    return await db.scalar(
        select(func.count()).select_from(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id, PasswordResetToken.created_at >= since
        )
    ) or 0


async def _void_open(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Every unused, unexpired secret of the user ends now."""
    now = _now()
    await db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
        .values(expires_at=now)
    )


async def request_reset(db: AsyncSession, channel: str, identifier: str) -> bool:
    """Send a reset secret if the account exists, is active, can be reached on
    the channel and has not asked too often. Returns whether one was sent —
    for the tests; the API answers the same either way. Commits."""
    user = await (find_by_email(db, identifier) if channel == EMAIL else find_by_phone(db, identifier))
    if user is None or not user.is_active:
        return False
    number = normalize_tr_mobile(user.phone) if channel == SMS else None
    if channel == SMS and number is None:
        return False
    if await _recent_requests(db, user) >= settings.PASSWORD_RESET_MAX_REQUESTS:
        return False

    await _void_open(db, user.id)
    ttl = settings.PASSWORD_RESET_TTL_MINUTES
    expires = _now() + timedelta(minutes=ttl)
    if channel == EMAIL:
        token = secrets.token_urlsafe(32)
        db.add(PasswordResetToken(user_id=user.id, channel=EMAIL, token_hash=hash_link_token(token), expires_at=expires))
        link = settings.PASSWORD_RESET_URL.format(token=token)
        await dispatch.send_email(
            db,
            to=user.email,
            subject=f"{settings.SHOP_NAME} şifre yenileme",
            text=(
                f"Merhaba{(' ' + user.full_name) if user.full_name else ''},\n\n"
                f"Şifrenizi yenilemek için aşağıdaki bağlantıyı açın. Bağlantı {ttl} dakika geçerlidir ve "
                f"yalnızca bir kez kullanılabilir.\n\n{link}\n\n"
                "Bu isteği siz yapmadıysanız bu e-postayı dikkate almayın; şifreniz değişmez.\n\n"
                f"{settings.SHOP_NAME}"
            ),
            purpose=PURPOSE,
            user_id=user.id,
        )
    else:
        code = f"{secrets.randbelow(10**6):06d}"
        db.add(PasswordResetToken(user_id=user.id, channel=SMS, token_hash=hash_sms_code(user.id, code), expires_at=expires))
        await dispatch.send_sms(
            db,
            recipients=[(number, user.id)],
            text=f"{settings.SHOP_NAME} sifre yenileme kodunuz: {code}. {ttl} dk gecerlidir. Kimseyle paylasmayin.",
            purpose=PURPOSE,
        )
    await db.commit()
    return True


async def link_token_row(db: AsyncSession, token: str) -> PasswordResetToken | None:
    """The live e-mail token row for this link, if any."""
    return await db.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == hash_link_token(token),
            PasswordResetToken.channel == EMAIL,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > _now(),
        )
    )


async def _sms_token_row(db: AsyncSession, phone: str, code: str) -> PasswordResetToken | None:
    """The user's live SMS code, if this is it; a wrong code counts against it."""
    user = await find_by_phone(db, phone)
    if user is None:
        return None
    row = await db.scalar(
        select(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.channel == SMS,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > _now(),
        )
        .order_by(PasswordResetToken.created_at.desc())
        .limit(1)
    )
    if row is None:
        return None
    if secrets.compare_digest(row.token_hash, hash_sms_code(user.id, code.strip())):
        return row
    row.attempts += 1
    if row.attempts >= settings.PASSWORD_RESET_MAX_ATTEMPTS:
        row.expires_at = _now()
    await db.commit()
    return None


class PasswordTooShort(ValueError):
    """An admin account's new password is under ADMIN_PASSWORD_MIN; nothing was changed."""


async def reset_password(
    db: AsyncSession, *, new_password: str, token: str | None = None, phone: str | None = None, code: str | None = None
) -> bool:
    """Set the password with a live secret and spend it (and every other open
    one of the user). False when the secret is wrong, used or expired;
    PasswordTooShort, with the secret left unspent, when an admin account's
    new password is shorter than its minimum. Commits."""
    row = await link_token_row(db, token) if token else await _sms_token_row(db, phone or "", code or "")
    if row is None:
        return False
    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        return False
    # Customers may choose six characters; an account that runs the shop may not.
    if user.role in (UserRole.SUPER_ADMIN, UserRole.ADMIN) and len(new_password) < ADMIN_PASSWORD_MIN:
        raise PasswordTooShort
    user.hashed_password = hash_password(new_password)
    # Every session from before the reset ends with it: whoever had the old
    # password, or a token, is out.
    user.token_version = (user.token_version or 0) + 1
    row.used_at = _now()
    await db.flush()
    await _void_open(db, user.id)
    await db.commit()
    return True
