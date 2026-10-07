import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_optional_user
from app.core.database import get_db
from app.models.catalog import CurrencyType, Product, ProductCrossSell, ProductVariant, WarehouseType
from app.models.promotion import FlashSale
from app.models.user import CustomerSegment, SegmentType, User
from app.schemas.catalog import (
    CatalogPrice,
    CatalogProductDetail,
    CatalogProductRead,
    CatalogVariantRead,
    FlashSaleInfo,
    ProductRead,
)
from app.services.categories import category_subtree
from app.services.checkout_engine import warehouse_for_segment
from app.services.exchange_rates import Rates, RatesUnavailable, get_rates
from app.services.pricing_engine import pick_price_row, to_try
from app.services.flash_sales import discounted_price, excl_tax, live_flash_sales

# Read-only. Catalog writes live in admin_catalog.py behind admin auth.
router = APIRouter(prefix="/products", tags=["products"])


# Turkish I/İ/ı don't case-fold like ASCII under ILIKE (e.g. "nargile" misses
# "NARGİLE"), so fold both sides to plain lowercase "i" before comparing.
_TR_I = str.maketrans("İIı", "iii")


def _fold_tr(text: str) -> str:
    return text.translate(_TR_I).lower()


def _folded(column):
    return func.lower(func.translate(column, "İIı", "iii"))


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def apply_catalog_filters(stmt, *, category: str | None, q: str | None, published_only: bool = False):
    """Filter by category slug (a parent includes its subcategories) and
    Turkish-aware free-text search.

    `published_only` is for the storefront; the admin lists see every product,
    published or not, because that is where publishing is switched.
    """
    if published_only:
        stmt = stmt.where(Product.is_published.is_(True))
    if category:
        stmt = stmt.where(Product.category_id.in_(category_subtree(category)))
    if q and q.strip():
        pattern = f"%{_escape_like(_fold_tr(q.strip()))}%"
        stmt = stmt.where(
            or_(
                *(
                    _folded(col).like(pattern, escape="\\")
                    for col in (Product.name, Product.brand, Product.base_code)
                )
            )
        )
    return stmt


def _price_for(variant: ProductVariant, segment_id: int, rates: Rates | None) -> tuple[CatalogPrice | None, CatalogPrice | None]:
    """(price in TRY, the foreign price it came from).

    The second is None whenever the admin priced this segment in TRY, which is the
    usual case; the storefront shows both when it is set.
    """
    row = pick_price_row(variant.prices, segment_id)
    if row is None:
        return None, None
    entered = CatalogPrice.model_validate(row, from_attributes=True)
    if CurrencyType(row.currency) == CurrencyType.TRY:
        return entered, None
    incl = to_try(row.price_incl_tax, row.currency, rates)
    excl = to_try(row.price_excl_tax, row.currency, rates)
    if incl is None or excl is None:
        return None, entered  # TCMB unreachable: show the foreign figure only
    return (
        CatalogPrice(currency=CurrencyType.TRY, price_excl_tax=excl, price_incl_tax=incl, vat_rate=row.vat_rate),
        entered,
    )


async def _rates_if_needed(products: list[Product]) -> Rates | None:
    """Fetch TCMB rates only when some price on the page is in a foreign currency."""
    if not any(
        CurrencyType(price.currency) != CurrencyType.TRY
        for product in products
        for variant in product.variants
        for price in variant.prices
    ):
        return None
    try:
        return await get_rates()
    except RatesUnavailable:
        return None


def _flash_sale_for(price: CatalogPrice | None, sale: FlashSale | None) -> FlashSaleInfo | None:
    """The sale as this caller would be charged it, or None if it doesn't beat their price."""
    if price is None or sale is None:
        return None
    incl = discounted_price(price.price_incl_tax, sale)
    if incl is None:
        return None
    return FlashSaleInfo(
        price=CatalogPrice(
            currency=price.currency,
            price_excl_tax=excl_tax(incl, price.vat_rate),
            price_incl_tax=incl,
            vat_rate=price.vat_rate,
        ),
        ends_at=sale.ends_at,
    )


def _in_stock(variant: ProductVariant, warehouse: WarehouseType) -> bool:
    return any(i.warehouse_type == warehouse and i.quantity > 0 for i in variant.inventory)


async def _catalog_view(
    db: AsyncSession, user: User | None, products: list[Product]
) -> list[CatalogProductRead]:
    """Price products for the caller's segment (RETAIL when anonymous), matching
    what checkout will charge, including live flash sales."""
    segments = {s.id: s.code for s in (await db.execute(select(CustomerSegment))).scalars()}
    retail_id = next(sid for sid, code in segments.items() if code == SegmentType.RETAIL)
    segment_id = (user.segment_id if user else None) or retail_id
    warehouse = warehouse_for_segment(segments.get(segment_id))
    rates = await _rates_if_needed(products)
    sales = await live_flash_sales(
        db, (v.id for p in products for v in p.variants), datetime.now(timezone.utc)
    )

    views = []
    for product in products:
        variants = []
        for v in sorted(product.variants, key=lambda v: v.sku):
            price, origin = _price_for(v, segment_id, rates)
            variants.append(
                CatalogVariantRead(
                    id=v.id,
                    sku=v.sku,
                    attributes=v.attributes,
                    price=price,
                    origin_price=origin,
                    list_price=_price_for(v, retail_id, rates)[0],
                    flash_sale=_flash_sale_for(price, sales.get(v.id)),
                    in_stock=_in_stock(v, warehouse),
                )
            )
        views.append(CatalogProductRead(**ProductRead.model_validate(product).model_dump(), variants=variants))
    return views


_VARIANT_LOADS = (
    selectinload(Product.variants).selectinload(ProductVariant.prices),
    selectinload(Product.variants).selectinload(ProductVariant.inventory),
)


@router.get("/", response_model=list[CatalogProductRead])
async def list_products(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    category: str | None = Query(None, max_length=180, description="Category slug"),
    q: str | None = Query(None, max_length=100, description="Search name, brand or code"),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Catalog page, priced for the caller (see _catalog_view)."""
    stmt = apply_catalog_filters(select(Product), category=category, q=q, published_only=True)
    result = await db.execute(
        stmt.options(*_VARIANT_LOADS).order_by(Product.name, Product.id).offset(skip).limit(limit)
    )
    return await _catalog_view(db, user, list(result.scalars().all()))


@router.get("/flash-sales", response_model=list[CatalogProductRead])
async def list_flash_sale_products(
    limit: int = Query(12, ge=1, le=50),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """The "Fırsat Ürünleri" list: every product whose opportunity switch is on.

    A deal whose deadline has passed, or whose deal stock has run out, stays on
    the list — it simply has no claimable price any more ("Fırsat Kaçtı"), which
    the caller sees as a missing `flash_sale`. Only the admin switching the
    opportunity off takes a product off the list. Claimable deals come first,
    then the soonest deadline, with the undated ones last.
    """
    now = datetime.now(timezone.utc)
    missed = or_(
        Product.opportunity_stock == 0,
        and_(Product.opportunity_end_date.is_not(None), Product.opportunity_end_date <= now),
    )
    result = await db.execute(
        select(Product)
        .where(Product.is_opportunity.is_(True), Product.is_published.is_(True))
        .order_by(missed.asc(), Product.opportunity_end_date.asc().nulls_last(), Product.id)
        .options(*_VARIANT_LOADS)
        .limit(limit)
    )
    return await _catalog_view(db, user, list(result.scalars().all()))


@router.get("/{product_id}", response_model=CatalogProductDetail)
async def get_product(
    product_id: uuid.UUID,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Product detail page, priced like the catalog (caller's segment, live sale,
    stock), together with its "birlikte satılanlar" — priced the same way, so the
    cards beside the product can be added to the cart straight from here."""
    product = await db.scalar(
        select(Product)
        .where(Product.id == product_id, Product.is_published.is_(True))
        .options(*_VARIANT_LOADS)
    )
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    pinned = (
        await db.execute(
            select(Product)
            .join(ProductCrossSell, ProductCrossSell.related_product_id == Product.id)
            .where(ProductCrossSell.product_id == product_id, Product.is_published.is_(True))
            .order_by(ProductCrossSell.position)
            .options(*_VARIANT_LOADS)
        )
    ).scalars().all()

    # One view call, so every card on the page is priced against the same segment
    # and the same rates.
    views = await _catalog_view(db, user, [product, *pinned])
    return CatalogProductDetail(**views[0].model_dump(), cross_sells=views[1:])
