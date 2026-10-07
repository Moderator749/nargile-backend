"""Flash-sale ("Fırsat Ürünleri") price resolution, shared by catalog and checkout."""

import uuid
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Product, ProductVariant
from app.models.promotion import FlashSale


async def live_flash_sales(
    db: AsyncSession, variant_ids: Iterable[uuid.UUID], now: datetime
) -> dict[uuid.UUID, FlashSale]:
    """Claimable offers at `now` for the given variants, keyed by variant id.

    An offer is claimable while its product's "Fırsat" switch is on, its deal
    stock has not run out (null = no quota) and it is inside its window — and a
    null `ends_at` means there is no deadline. Everything else (deadline passed,
    quota exhausted) is a "Fırsat Kaçtı": still listed, no longer priced.
    """
    ids = list(variant_ids)
    if not ids:
        return {}
    result = await db.execute(
        select(FlashSale)
        .join(ProductVariant, ProductVariant.id == FlashSale.variant_id)
        .join(Product, Product.id == ProductVariant.product_id)
        .where(
            FlashSale.variant_id.in_(ids),
            FlashSale.is_active.is_(True),
            FlashSale.starts_at <= now,
            or_(FlashSale.ends_at.is_(None), FlashSale.ends_at > now),
            Product.is_opportunity.is_(True),
            or_(Product.opportunity_stock.is_(None), Product.opportunity_stock > 0),
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
