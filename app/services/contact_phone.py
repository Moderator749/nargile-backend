"""The account's own phone number: who else has it, and filling it in.

A Turkish mobile on an account is how "Şifremi unuttum" finds it by SMS, and
that only works while one account has the number (password_reset.find_by_phone
refuses a number it cannot tell apart). So a number already on another account
is never written onto this one; a landline or a foreign number has no such use
and is not checked.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services.notify.phone import normalize_tr_mobile


async def mobile_taken(db: AsyncSession, phone: str, user_id: uuid.UUID) -> bool:
    """Whether another account holds this Turkish mobile number."""
    number = normalize_tr_mobile(phone)
    if number is None:
        return False
    last10 = func.right(func.regexp_replace(User.phone, r"\D", "", "g"), 10)
    return await db.scalar(select(User.id).where(last10 == number[2:], User.id != user_id).limit(1)) is not None


async def fill_missing_phone(db: AsyncSession, user: User, phone: str | None) -> bool:
    """Put the number an order was placed with on an account that has none.

    Only an empty field is filled: a number the customer chose stays theirs to
    change in "Hesabım", not something one order rewrites. Not committed.
    """
    given = (phone or "").strip()
    if user.phone or not given or await mobile_taken(db, given, user.id):
        return False
    user.phone = given
    return True
