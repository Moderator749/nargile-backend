"""Flash-sale ("Fırsat Ürünleri") price resolution, shared by catalog and checkout."""

import uuid
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.promotion import FlashSale


async def live_flash_sales(
    db: AsyncSession, variant_ids: Iterable[uuid.UUID], now: datetime
) -> dict[uuid.UUID, FlashSale]:
    """Sales live at `now` for the given variants, keyed by variant id."""
    ids = list(variant_ids)
    if not ids:
        return {}
    result = await db.execute(
        select(FlashSale).where(
            FlashSale.variant_id.in_(ids),
            FlashSale.is_active.is_(True),
            FlashSale.starts_at <= now,
            FlashSale.ends_at > now,
        )
    )
    return {sale.variant_id: sale for sale in result.scalars()}


def discounted_price(regular_incl_tax: Decimal, sale: FlashSale | None) -> Decimal | None:
    """The sale price if it beats the viewer's regular price, else None.

    A dealer whose segment price is already lower keeps that price: a sale never
    raises anyone's price.
    """
    if sale is None or sale.sale_price_incl_tax >= regular_incl_tax:
        return None
    return sale.sale_price_incl_tax


def excl_tax(incl_tax: Decimal, vat_rate: Decimal) -> Decimal:
    return (incl_tax / (1 + vat_rate / 100)).quantize(Decimal("0.01"))
