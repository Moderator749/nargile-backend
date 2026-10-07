"""Price resolution: which row to charge, and what it is worth in TRY.

A segment's price may be entered in TRY, USD or EUR. TRY is charged as typed;
a foreign price is converted at the day's TCMB rate, because an order is always
booked in TRY (see app.services.exchange_rates).
"""

from collections.abc import Iterable
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import CurrencyType, VariantPrice
from app.services.exchange_rates import Rates

# TRY first: if the admin typed a TRY price, that is the price. Otherwise the
# foreign row they did fill, tried in this order.
CURRENCY_PREFERENCE = (CurrencyType.TRY, CurrencyType.USD, CurrencyType.EUR)


def pick_price_row(prices: Iterable[VariantPrice], segment_id: int) -> VariantPrice | None:
    """The row that prices this variant for a segment, or None if it isn't sold to it."""
    rows = {CurrencyType(p.currency): p for p in prices if p.segment_id == segment_id}
    return next((rows[c] for c in CURRENCY_PREFERENCE if c in rows), None)


def to_try(amount: Decimal, currency: CurrencyType | str, rates: Rates | None) -> Decimal | None:
    """`amount` in TRY, or None when the rate needed for it is unavailable."""
    currency = CurrencyType(currency)
    if currency == CurrencyType.TRY:
        return amount
    rate = rates.try_per_unit.get(currency.value) if rates else None
    if not rate or rate <= 0:
        return None
    return (amount * rate).quantize(Decimal("0.01"))


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
