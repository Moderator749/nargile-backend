"""The settings an admin can change from the panel, and their defaults.

Every reader goes through here rather than through the table, because a setting
has to have an answer: a row that is missing, blank or hand-edited into
nonsense falls back to the default below instead of failing the cart. The
default is also what the panel shows before anything has been saved.
"""

from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import ShopSetting

#: "İstanbul içi 20000 ve üzeri alışverişlerde ücretsiz teslimat" — the cart
#: value from which the shop's own van carries a firm's order for nothing.
ISTANBUL_FREE_MIN_KEY = "istanbul_free_min_subtotal"
ISTANBUL_FREE_MIN_DEFAULT = Decimal("20000.00")
#: As high as the panel will let the limit go, so a slipped keypress cannot
#: quietly take the option away for good.
ISTANBUL_FREE_MIN_MAX = Decimal("1000000.00")


def _as_decimal(value: str | None, default: Decimal) -> Decimal:
    if value is None:
        return default
    try:
        found = Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        return default
    return default if found < 0 else found.quantize(Decimal("0.01"))


async def read_setting(db: AsyncSession, key: str) -> str | None:
    """The stored text for a key, or None when nothing has been saved."""
    return (
        await db.execute(select(ShopSetting.value).where(ShopSetting.key == key))
    ).scalar_one_or_none()


async def istanbul_free_minimum(db: AsyncSession) -> Decimal:
    """The subtotal from which İstanbul delivery is free for a firm."""
    return _as_decimal(await read_setting(db, ISTANBUL_FREE_MIN_KEY), ISTANBUL_FREE_MIN_DEFAULT)


async def save_setting(db: AsyncSession, key: str, value: str, admin_id=None) -> ShopSetting:
    """Write a setting, creating its row the first time. The caller commits."""
    row = await db.get(ShopSetting, key)
    if row is None:
        row = ShopSetting(key=key)
        db.add(row)
    row.value = value
    row.updated_by = admin_id
    return row
