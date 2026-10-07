"""Loyalty balance helper (kept for existing callers; see app.services.wallet)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.wallet import get_wallet


async def get_user_loyalty_balance(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Usable points: unexpired earned points minus non-cancelled spending (FIFO by expiry)."""
    return (await get_wallet(db, user_id)).balance
