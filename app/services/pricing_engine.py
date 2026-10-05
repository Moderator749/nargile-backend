from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import VariantPrice


async def get_segment_price(
    db: AsyncSession,
    variant_id,
    segment_id: int,
    currency: str = "TRY",
) -> VariantPrice | None:
    stmt = select(VariantPrice).where(
        VariantPrice.variant_id == variant_id,
        VariantPrice.segment_id == segment_id,
        VariantPrice.currency == currency,
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


def calculate_line_total(unit_price: Decimal, quantity: int) -> Decimal:
    return (unit_price * quantity).quantize(Decimal("0.01"))
