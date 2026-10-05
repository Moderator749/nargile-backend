"""Loyalty (ParaPuan) balance and validation helpers."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.loyalty import LoyaltyLedger, TransactionType


async def get_user_loyalty_balance(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Return the user's usable ParaPuan balance.

    Sums all EARNED entries whose expires_at > now (or is NULL),
    minus all SPENT entries.
    """
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(
            func.coalesce(
                func.sum(
                    case(
                        # EARNED points that haven't expired
                        (
                            (LoyaltyLedger.transaction_type == TransactionType.EARNED)
                            & (
                                (LoyaltyLedger.expires_at.is_(None))
                                | (LoyaltyLedger.expires_at > now)
                            ),
                            LoyaltyLedger.amount,
                        ),
                        # SPENT points (amount is positive, subtract them)
                        (
                            LoyaltyLedger.transaction_type == TransactionType.SPENT,
                            -LoyaltyLedger.amount,
                        ),
                        else_=0,
                    )
                ),
                0,
            )
        ).where(LoyaltyLedger.user_id == user_id)
    )
    return int(result.scalar_one())
