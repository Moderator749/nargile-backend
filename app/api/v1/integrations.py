"""Routes for programs on this server, signed in as service accounts
(app.api.service_auth): local calls only, each route with its own scope.

GET /integrations/price-intel/catalog — what the price-intelligence service
(radar) watches: every live product outside the shop's own brands, with its
colours' stock, cost and Perakende price, its last applied Cetvel row and its
recent sales; plus the day's TCMB rates and the havale ladder, to turn those
into a worst-case TL figure. Read-only: nothing here changes a price.

The whole list is sent every time, a page at a time (`after`, `limit`), not
"changed since": a price, a stock count or a cost changes in its own table
without touching the product's updated_at, and a feed that missed one of
those would show the market against a price the shop no longer charges.
About 1,500 products is a few hundred kilobytes, on the loopback.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.service_auth import require_service
from app.core.config import settings
from app.core.database import get_db
from app.models.brand import Brand
from app.models.catalog import Category, CurrencyType, Inventory, Product, ProductVariant, VariantPrice
from app.models.order import PAID_STATUSES, Order, OrderItem
from app.models.pricing import PricingRecord
from app.models.service_account import PRICE_READ, ServiceAccount
from app.models.user import CustomerSegment, SegmentType
from app.schemas.integrations import (
    CatalogCategory,
    CatalogPage,
    CatalogProduct,
    CatalogSales,
    CatalogVariant,
    CetvelRecord,
    HavaleTier,
    Rates,
    RetailPrice,
    SalesWindow,
)
from app.services.categories import ancestors
from app.services.checkout_engine import CENTRAL_STOCK
from app.services.exchange_rates import RatesUnavailable, get_rates
from app.services.store_parameters import load_parameters

router = APIRouter(prefix="/integrations/price-intel", tags=["integrations"])

WINDOWS = (7, 30, 90)
CENT = Decimal("0.01")


def _retail(rows: list[VariantPrice]) -> RetailPrice | None:
    """The TRY row when there is one (checkout charges it), else the other."""
    if not rows:
        return None
    row = next((r for r in rows if CurrencyType(r.currency) == CurrencyType.TRY), rows[0])
    return RetailPrice(
        currency=CurrencyType(row.currency).value,
        price_excl_tax=row.price_excl_tax,
        price_incl_tax=row.price_incl_tax,
        vat_rate=row.vat_rate,
    )


async def _sales(db: AsyncSession, product_ids: list[uuid.UUID]) -> dict[uuid.UUID, CatalogSales]:
    now = datetime.now(timezone.utc)
    when = func.coalesce(Order.paid_at, Order.created_at)
    retail = func.coalesce(Order.segment_code, SegmentType.RETAIL) == SegmentType.RETAIL
    columns = []
    for days in WINDOWS:
        inside = when >= now - timedelta(days=days)
        columns += [
            func.coalesce(func.sum(case((inside, OrderItem.quantity), else_=0)), 0),
            func.coalesce(func.sum(case((and_(inside, retail), OrderItem.quantity), else_=0)), 0),
            func.coalesce(func.sum(case((inside, OrderItem.line_total), else_=0)), 0),
        ]
    rows = await db.execute(
        select(ProductVariant.product_id, *columns)
        .join(OrderItem, OrderItem.variant_id == ProductVariant.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            ProductVariant.product_id.in_(product_ids),
            Order.status.in_(PAID_STATUSES),
            Order.is_quote.is_(False),
            when >= now - timedelta(days=max(WINDOWS)),
        )
        .group_by(ProductVariant.product_id)
    )
    out = {}
    for product_id, *figures in rows.all():
        windows = [
            SalesWindow(quantity=int(q), retail_quantity=int(rq), revenue=Decimal(rev).quantize(CENT))
            for q, rq, rev in zip(figures[0::3], figures[1::3], figures[2::3])
        ]
        out[product_id] = CatalogSales(d7=windows[0], d30=windows[1], d90=windows[2])
    return out


async def _cetvel(db: AsyncSession, product_ids: list[uuid.UUID]) -> dict[uuid.UUID, CetvelRecord]:
    """Each product's latest applied Cetvel row (its applied_summary names it)."""
    applied_to = PricingRecord.applied_summary["product_id"].astext
    rows = await db.execute(
        select(applied_to, PricingRecord.id, PricingRecord.applied_at, PricingRecord.updated_at)
        .where(PricingRecord.applied_at.is_not(None), applied_to.in_([str(i) for i in product_ids]))
        .ext(distinct_on(applied_to))
        .order_by(applied_to, PricingRecord.applied_at.desc())
    )
    return {
        uuid.UUID(pid): CetvelRecord(id=rid, applied_at=applied_at, updated_at=updated_at)
        for pid, rid, applied_at, updated_at in rows.all()
    }


async def _rates() -> Rates | None:
    try:
        rates = await get_rates()
    except RatesUnavailable:
        return None
    return Rates(bulletin_date=rates.bulletin_date, try_per_unit=dict(rates.try_per_unit))


@router.get("/catalog", response_model=CatalogPage)
async def catalog(
    after: uuid.UUID | None = Query(None, description="next_after of the previous page"),
    limit: int = Query(500, ge=1, le=1000),
    _service: ServiceAccount = Depends(require_service(PRICE_READ)),
    db: AsyncSession = Depends(get_db),
):
    excluded_slugs = list(settings.PRICE_INTEL_EXCLUDED_BRAND_SLUGS)
    excluded_ids = (await db.scalars(select(Brand.id).where(Brand.slug.in_(excluded_slugs)))).all()

    query = (
        select(Product, Brand.name, Brand.slug)
        .outerjoin(Brand, Brand.id == Product.brand_id)
        .where(Product.is_deleted.is_(False))
        .order_by(Product.id)
        .limit(limit + 1)
    )
    if excluded_ids:
        query = query.where((Product.brand_id.is_(None)) | (Product.brand_id.not_in(excluded_ids)))
    if after is not None:
        query = query.where(Product.id > after)
    rows = (await db.execute(query)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    product_ids = [p.id for p, _, _ in rows]

    variants: dict[uuid.UUID, list[ProductVariant]] = {}
    for v in (
        await db.scalars(
            select(ProductVariant)
            .where(ProductVariant.product_id.in_(product_ids), ProductVariant.is_active.is_(True))
            .order_by(ProductVariant.product_id, ProductVariant.position, ProductVariant.sku)
        )
    ).all():
        variants.setdefault(v.product_id, []).append(v)
    variant_ids = [v.id for vs in variants.values() for v in vs]

    stock = dict(
        (
            await db.execute(
                select(Inventory.variant_id, Inventory.quantity).where(
                    Inventory.variant_id.in_(variant_ids), Inventory.warehouse_type == CENTRAL_STOCK
                )
            )
        ).all()
    )
    retail_rows: dict[uuid.UUID, list[VariantPrice]] = {}
    for row in (
        await db.scalars(
            select(VariantPrice)
            .join(CustomerSegment, CustomerSegment.id == VariantPrice.segment_id)
            .where(VariantPrice.variant_id.in_(variant_ids), CustomerSegment.code == SegmentType.RETAIL)
        )
    ).all():
        retail_rows.setdefault(row.variant_id, []).append(row)

    categories = {c.id: c for c in (await db.scalars(select(Category))).all()}
    parents = {c.id: c.parent_id for c in categories.values()}
    sales = await _sales(db, product_ids)
    cetvel = await _cetvel(db, product_ids)
    nothing_sold = SalesWindow(quantity=0, retail_quantity=0, revenue=Decimal(0).quantize(CENT))

    products = []
    for product, brand_name, brand_slug in rows:
        own = [
            CatalogVariant(
                id=v.id,
                sku=v.sku,
                color_name=v.color_name,
                stock=stock.get(v.id, 0),
                cost_price=v.cost_price,
                cost_currency=CurrencyType(v.cost_currency).value,
                retail=_retail(retail_rows.get(v.id, [])),
            )
            for v in variants.get(product.id, [])
        ]
        category = categories.get(product.category_id) if product.category_id else None
        path = [categories[i].name for i in reversed(ancestors(category.id, parents)) if i in categories] if category else []
        products.append(
            CatalogProduct(
                id=product.id,
                code=product.base_code,
                name=product.name,
                slug=product.slug,
                brand_id=product.brand_id,
                brand=brand_name,
                brand_slug=brand_slug,
                category=CatalogCategory(id=category.id, name=category.name, slug=category.slug) if category else None,
                category_path=[*path, category.name] if category else [],
                image_url=product.image_url,
                is_published=product.is_published,
                retail=own[0].retail if own else None,
                retail_uniform=len({(v.retail.currency, v.retail.price_incl_tax) if v.retail else None for v in own}) <= 1,
                variants=own,
                cetvel=cetvel.get(product.id),
                sales=sales.get(product.id) or CatalogSales(d7=nothing_sold, d30=nothing_sold, d90=nothing_sold),
            )
        )

    params = await load_parameters(db)
    return CatalogPage(
        generated_at=datetime.now(timezone.utc),
        rates=await _rates(),
        havale_tiers=[HavaleTier(threshold=t.threshold, rate=t.rate) for t in params.bank_transfer_tiers],
        excluded_brand_slugs=excluded_slugs,
        products=products,
        next_after=product_ids[-1] if more else None,
    )
