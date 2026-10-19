import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user, get_optional_user
from app.core.database import get_db
from app.models.catalog import CurrencyType, Product, ProductCrossSell, ProductVariant
from app.models.order import Order, OrderItem, OrderStatus
from app.models.promotion import FlashSale
from app.models.user import CustomerSegment, SegmentType, User
from app.schemas.catalog import (
    CatalogPrice,
    CatalogProductDetail,
    CatalogProductRead,
    CatalogVariantRead,
    FlashSaleInfo,
    PriceListItem,
    PriceListRequest,
    ProductRead,
    SultanPuanShowcase,
)
from app.services import slug_redirects
from app.services.categories import category_subtree, memberships, products_in
from app.services.checkout_engine import stock_source
from app.services.exchange_rates import Rates, RatesUnavailable, get_rates
from app.services.pricing_engine import pick_price_row, to_try
from app.services.flash_sales import discounted_price, excl_tax, live_flash_sales
from app.services.store_parameters import load_parameters

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
        stmt = stmt.where(Product.id.in_(products_in(category_subtree(category))))
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


async def _catalog_view(
    db: AsyncSession, user: User | None, products: list[Product]
) -> list[CatalogProductRead]:
    """Price products for the caller's segment (RETAIL when anonymous), matching
    what checkout will charge, including live flash sales.

    Every live colour of a product comes back as its own variant, in the admin's
    order, each with its own stock flag — the product page's colour picker is a
    choice between these. A retired colour is left out: it exists for the orders
    that contain it, not to be bought again.
    """
    segments = {s.id: s.code for s in (await db.execute(select(CustomerSegment))).scalars()}
    retail_id = next(sid for sid, code in segments.items() if code == SegmentType.RETAIL)
    segment_id = (user.segment_id if user else None) or retail_id
    segment = segments.get(segment_id)
    rates = await _rates_if_needed(products)
    sales = await live_flash_sales(
        db, (v.id for p in products for v in p.variants), datetime.now(timezone.utc)
    )

    views = []
    for product in products:
        variants = []
        for v in sorted(
            (v for v in product.variants if v.is_active), key=lambda v: (v.position, v.sku)
        ):
            price, origin = _price_for(v, segment_id, rates)
            # The segment's own quota only.
            _, available = stock_source(v.inventory, segment)
            variants.append(
                CatalogVariantRead(
                    id=v.id,
                    sku=v.sku,
                    color_name=v.color_name,
                    attributes=v.attributes,
                    price=price,
                    origin_price=origin,
                    list_price=_price_for(v, retail_id, rates)[0],
                    flash_sale=_flash_sale_for(price, sales.get(v.id)),
                    in_stock=available > 0,
                    available_quantity=available if user else None,
                )
            )
        view = ProductRead.model_validate(product).model_dump()
        # The labels the live variants carry, so the picker and the pills agree
        # even if a colour was retired after the product's own list was written.
        view["color_names"] = [v.color_name for v in variants if v.color_name]
        views.append(CatalogProductRead(**view, variants=variants))
    return views


_VARIANT_LOADS = (
    selectinload(Product.variants).selectinload(ProductVariant.prices),
    selectinload(Product.variants).selectinload(ProductVariant.inventory),
)


@router.get("", response_model=list[CatalogProductRead])
async def list_products(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    category: str | None = Query(None, max_length=180, description="Category slug"),
    q: str | None = Query(None, max_length=100, description="Search name, brand or code"),
    ids: list[uuid.UUID] | None = Query(None, max_length=100, description="Only these products"),
    sort: Literal["name", "newest"] = Query("name", description="By name, or the newest first"),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Catalog page, priced for the caller (see _catalog_view).

    `ids` narrows it to a set the page already chose (the product page's
    "Benzer Ürünler", re-priced for a signed-in viewer). `sort=newest` is the
    homepage's "Yeni Gelenler": the latest added first — the imported catalog
    shares one creation time, so among those it falls back on the name."""
    stmt = apply_catalog_filters(select(Product), category=category, q=q, published_only=True)
    if ids:
        stmt = stmt.where(Product.id.in_(ids))
    order = (Product.name, Product.id)
    if sort == "newest":
        order = (Product.created_at.desc(), *order)
    result = await db.execute(
        stmt.options(*_VARIANT_LOADS).order_by(*order).offset(skip).limit(limit)
    )
    return await _catalog_view(db, user, list(result.scalars().all()))


# Which price groups a B2B account may put in a catalogue for its own customers:
# the groups below it, and retail — never a group above it, whose buying price
# it has no business seeing. A kafe sends its own price and retail.
PRICE_LIST_GROUPS: dict[SegmentType, frozenset[SegmentType]] = {
    SegmentType.DEALER: frozenset({SegmentType.WHOLESALE, SegmentType.CAFE, SegmentType.RETAIL}),
    SegmentType.WHOLESALE: frozenset({SegmentType.CAFE, SegmentType.RETAIL}),
    SegmentType.CAFE: frozenset({SegmentType.CAFE, SegmentType.RETAIL}),
}


@router.post("/price-list", response_model=list[PriceListItem])
async def price_list(
    payload: PriceListRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Prices for a B2B account's PDF catalogue ("Fiyat Kataloğu İndir").

    The products are the ones on the customer's screen, kept in their order;
    unpublished ones are left out. Each price is the regular list price of the
    group (no flash sales: a catalogue outlives a sale), in TRY — a price kept
    in USD/EUR converted at the day's TCMB rate, as checkout does. Which groups
    may be asked for depends on the account (PRICE_LIST_GROUPS), and that is
    checked here, not only in the dialog that offers them.
    """
    segments = {s.code: s.id for s in (await db.execute(select(CustomerSegment))).scalars()}
    own = next((code for code, sid in segments.items() if sid == user.segment_id), None)
    allowed = PRICE_LIST_GROUPS.get(own) if own else None
    if allowed is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Fiyat kataloğu yalnızca bayi, toptan ve kafe hesaplarına açıktır")
    asked = set(payload.groups)
    if not asked <= allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bu fiyat grubu hesabınız için kataloğa eklenemez")
    return await build_price_list(db, payload.product_ids, payload.groups)


async def build_price_list(
    db: AsyncSession, product_ids: list[uuid.UUID], groups: list[SegmentType]
) -> list[PriceListItem]:
    """A price catalogue's rows: the published products among these, in the
    order given (each once), with each group's regular list price in TRY (no
    flash sales: a catalogue outlives a sale; USD/EUR at the day's TCMB rate).
    A group the product is not sold to is left out of its prices. Who may ask
    for which group is the caller's to decide."""
    segments = {s.code: s.id for s in (await db.execute(select(CustomerSegment))).scalars()}
    rows = (
        await db.execute(
            select(Product)
            .where(Product.id.in_(product_ids), Product.is_published.is_(True))
            .options(selectinload(Product.variants).selectinload(ProductVariant.prices))
        )
    ).scalars().all()
    by_id = {p.id: p for p in rows}
    rates = await _rates_if_needed(list(rows))
    items: list[PriceListItem] = []
    for product_id in dict.fromkeys(product_ids):  # screen order, each once
        product = by_id.get(product_id)
        if product is None:
            continue
        # Prices are product-wide (the sheet writes every colour the same): the
        # first live colour speaks for the product.
        variant = min(
            (v for v in product.variants if v.is_active), key=lambda v: (v.position, v.sku), default=None
        )
        prices = {}
        if variant is not None:
            for group in groups:
                price, _ = _price_for(variant, segments[group], rates)
                if price is not None and price.currency == CurrencyType.TRY:
                    prices[group] = price
        items.append(PriceListItem(
            product_id=product.id, code=product.base_code, name=product.name,
            image_url=product.image_url, prices=prices,
        ))
    return items


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


# Orders that count as sold: approved (paid) or shipped. Pending, cancelled and
# failed ones never took the goods, and a quote (an export request) moves no
# stock — it is not a sale.
SOLD_STATUSES = (OrderStatus.APPROVED, OrderStatus.SHIPPED)


@router.get("/bestsellers", response_model=list[CatalogProductRead])
async def list_bestsellers(
    limit: int = Query(10, ge=1, le=50),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """The homepage's "Çok Satanlar": published products by the quantity sold,
    every colour counted together, the most first.

    Only products that have sold are listed; a list shorter than asked for is
    the caller's to top up (the homepage fills it from its featured category).
    """
    sold = (
        select(ProductVariant.product_id, func.sum(OrderItem.quantity).label("sold"))
        .join(OrderItem, OrderItem.variant_id == ProductVariant.id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.status.in_(SOLD_STATUSES), Order.is_quote.is_(False))
        .group_by(ProductVariant.product_id)
        .subquery()
    )
    result = await db.execute(
        select(Product)
        .join(sold, sold.c.product_id == Product.id)
        .where(Product.is_published.is_(True))
        .order_by(sold.c.sold.desc(), Product.name, Product.id)
        .options(*_VARIANT_LOADS)
        .limit(limit)
    )
    return await _catalog_view(db, user, list(result.scalars().all()))


def _current_retail_price(product: CatalogProductRead):
    """What the product's card shows a retail buyer, in TRY with KDV: the colour
    it opens on (the first in stock, else the first), at its live deal price if
    one beats the regular price. None when that colour has no retail price."""
    variant = next((v for v in product.variants if v.in_stock), None) or next(iter(product.variants), None)
    if variant is None:
        return None
    price = variant.flash_sale.price if variant.flash_sale else variant.price
    return price.price_incl_tax if price else None


@router.get("/sultanpuan", response_model=SultanPuanShowcase)
async def list_sultanpuan_products(db: AsyncSession = Depends(get_db)):
    """The homepage's Sultan Puan row: every published product whose current
    retail price (see `_current_retail_price`) is inside the standard earning
    band, bounds included — bought on its own, it earns the rate.

    Band and rate are read live from the parameters ("Parametreler"), so a new
    band is a new list. Priced as retail whoever asks: Sultan Puan is earned on
    retail orders only. Cheapest first. With the programme off, no products.
    """
    loyalty = (await load_parameters(db)).loyalty
    showcase = SultanPuanShowcase(
        enabled=loyalty.enabled,
        rate_percent=(loyalty.base_rate * 100).quantize(Decimal("0.01")),
        band_min=loyalty.band_min,
        band_max=loyalty.band_max,
    )
    if not loyalty.enabled:
        return showcase
    result = await db.execute(
        select(Product).where(Product.is_published.is_(True)).options(*_VARIANT_LOADS).order_by(Product.name, Product.id)
    )
    priced = [
        (price, product)
        for product in await _catalog_view(db, None, list(result.scalars().all()))
        if (price := _current_retail_price(product)) is not None and loyalty.band_min <= price <= loyalty.band_max
    ]
    showcase.products = [product for _, product in sorted(priced, key=lambda pair: pair[0])]
    return showcase


@router.get("/{product_ref}", response_model=CatalogProductDetail)
async def get_product(
    product_ref: str = Path(max_length=255, description="The product's slug, or its id"),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Product detail page, priced like the catalog (caller's segment, live sale,
    stock), together with its "birlikte satılanlar" — priced the same way, so the
    cards beside the product can be added to the cart straight from here.

    Read by slug, which is the page's address; an id still works (links from
    before slugs, the admin), and so does a slug the admin has since changed.
    Either way the answer's `slug` is the current one, which is where the page
    that asked otherwise sends its visitor for good."""
    try:
        match = Product.id == uuid.UUID(product_ref)
    except ValueError:
        slug = product_ref.lower()
        renamed = await slug_redirects.product_id_for(db, slug)
        match = or_(Product.slug == slug, Product.id == renamed) if renamed else Product.slug == slug
    product = await db.scalar(
        select(Product)
        .where(match, Product.is_published.is_(True))
        # A live slug wins over a redirect that happens to share it.
        .order_by((Product.slug == product_ref.lower()).desc())
        .options(*_VARIANT_LOADS)
        .limit(1)
    )
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    pinned = (
        await db.execute(
            select(Product)
            .join(ProductCrossSell, ProductCrossSell.related_product_id == Product.id)
            .where(ProductCrossSell.product_id == product.id, Product.is_published.is_(True))
            .order_by(ProductCrossSell.position)
            .options(*_VARIANT_LOADS)
        )
    ).scalars().all()

    # One view call, so every card on the page is priced against the same segment
    # and the same rates.
    views = await _catalog_view(db, user, [product, *pinned])
    listed = memberships().subquery()
    linked = set(
        (await db.execute(select(listed.c.category_id).where(listed.c.product_id == product.id))).scalars()
    )
    primary = [product.category_id] if product.category_id in linked else []
    return CatalogProductDetail(
        **views[0].model_dump(),
        cross_sells=views[1:],
        category_ids=primary + sorted(linked - set(primary)),
    )
