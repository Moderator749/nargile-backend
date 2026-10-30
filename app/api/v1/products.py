import re
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
from app.models.brand import Brand
from app.models.catalog import (
    Category,
    CategoryProductOrder,
    CurrencyType,
    Inventory,
    Product,
    ProductCrossSell,
    ProductVariant,
)
from app.models.order import PAID_STATUSES, Order, OrderItem
from app.models.promotion import FlashSale
from app.models.user import CustomerSegment, SegmentType, User
from app.schemas.catalog import (
    CatalogPrice,
    CatalogSummary,
    SearchSuggestions,
    SuggestedCategory,
    CatalogProductDetail,
    CatalogProductRead,
    CatalogVariantRead,
    FlashSaleInfo,
    PriceListItem,
    PriceListRequest,
    ProductRead,
    SultanPuanShowcase,
)
from app.schemas.cart import CartLineInfo, StockCheckLine, StockCheckRequest
from app.schemas.i18n import Locale
from app.services import localize, slug_redirects
from app.services.categories import category_subtree, memberships, products_in
from app.services.checkout_engine import CENTRAL_STOCK, HAVALE_SWITCH, shows_stock_count, stock_on_hand
from app.services.exchange_rates import Rates, RatesUnavailable, get_rates
from app.services.pricing_engine import pick_price_row, to_try
from app.services.flash_sales import discounted_price, excl_tax, live_flash_sales
from app.services.store_parameters import load_parameters

# Read-only. Catalog writes live in admin_catalog.py behind admin auth.
router = APIRouter(prefix="/products", tags=["products"])


# Turkish letters don't case-fold like ASCII under ILIKE ("nargile" misses
# "NARGİLE"), and a phone's keyboard types "lule" for "lüle", "komur" for
# "kömür": fold both sides to plain lowercase ASCII before comparing.
_TR_FROM = "İIıÖöÜüŞşÇçĞğÂâÎîÛû"
_TR_TO = "iiioouussccggaaiiuu"
_TR_FOLD = str.maketrans(_TR_FROM, _TR_TO)


def _fold_tr(text: str) -> str:
    return text.translate(_TR_FOLD).lower()


def _folded(column):
    return func.lower(func.translate(column, _TR_FROM, _TR_TO))


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _brand_terms(db: AsyncSession) -> list[tuple[re.Pattern[str], str]]:
    """The active brands, each as the whole word its page searches for (folded) and its name."""
    rows = (await db.execute(select(Brand.name, Brand.search_term).where(Brand.is_active.is_(True)))).all()
    return [
        (re.compile(rf"(?<![0-9a-z]){re.escape(_fold_tr(term.strip()))}(?![0-9a-z])"), name)
        for name, term in rows
        if term and term.strip()
    ]


def brand_in_name(name: str, terms: list[tuple[re.Pattern[str], str]]) -> str | None:
    """The brand a product's name carries, for one whose own brand field is empty.

    Most of the imported catalogue has none ("Alpha Hookah Echo Gold Noir"),
    though its brand page lists it, found by the brand's search term: the
    brand whose term comes first in the name, as a whole word, the longer term
    on a tie. None when no brand's term is in it.
    """
    folded = _fold_tr(name)
    best: tuple[tuple[int, int], str] | None = None
    for pattern, brand in terms:
        found = pattern.search(folded)
        if found and (best is None or (found.start(), -len(pattern.pattern)) < best[0]):
            best = ((found.start(), -len(pattern.pattern)), brand)
    return best[1] if best else None


#: How many of each the header search suggests while a visitor types.
SUGGESTED_PRODUCTS = 6
SUGGESTED_CATEGORIES = 4

#: The group of complete sets, listed first on a brand's page.
SETS_CATEGORY = "nargile-takimlari"

#: `lang` on the storefront's reads: the answer in English or Arabic (app.services.localize).
LANG = Query(None, description="en | ar — the product text in that language; Turkish when absent")


def apply_catalog_filters(
    stmt,
    *,
    category: str | None,
    q: str | None,
    published_only: bool = False,
    lang: str | None = None,
    brand: str | None = None,
):
    """Filter by category slug (a parent includes its subcategories) and
    Turkish-aware free-text search — in English or Arabic too, with `lang`, so
    "hookah bowl" finds the Lüleler.

    `published_only` is for the storefront; the admin lists see every product,
    published or not, because that is where publishing is switched.
    """
    if published_only:
        stmt = stmt.where(Product.is_published.is_(True))
    if category:
        stmt = stmt.where(Product.id.in_(products_in(category_subtree(category))))
    if brand:
        # A brand's page (/<brand-slug>): its products, by the brand they belong to.
        stmt = stmt.where(Product.brand_id == select(Brand.id).where(Brand.slug == brand).scalar_subquery())
    if q and q.strip():
        pattern = f"%{_escape_like(_fold_tr(q.strip()))}%"
        # A category's name finds what is filed under it: "lule" lists the Lüleler.
        named = select(Category.id).where(_folded(Category.name).like(pattern, escape="\\"))
        stmt = stmt.where(
            or_(
                *(
                    _folded(col).like(pattern, escape="\\")
                    for col in (
                        Product.name,
                        Product.brand,
                        Product.base_code,
                        *([Product.translations[lang]["name"].astext] if lang in ("en", "ar") else []),
                    )
                ),
                Product.id.in_(products_in(named)),
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


async def _price_lists(db: AsyncSession, user: User | None) -> tuple[int, SegmentType | None, int]:
    """(the caller's price list, its code, the retail list's id): RETAIL for a visitor."""
    segments = {s.id: s.code for s in (await db.execute(select(CustomerSegment))).scalars()}
    retail_id = next(sid for sid, code in segments.items() if code == SegmentType.RETAIL)
    segment_id = (user.segment_id if user else None) or retail_id
    return segment_id, segments.get(segment_id), retail_id


async def _catalog_view(
    db: AsyncSession, user: User | None, products: list[Product], lang: str | None = None
) -> list[CatalogProductRead]:
    """Price products for the caller's segment (RETAIL when anonymous), matching
    what checkout will charge, including live flash sales.

    Every live colour of a product comes back as its own variant, in the admin's
    order, each with its own stock flag — the product page's colour picker is a
    choice between these. A retired colour is left out: it exists for the orders
    that contain it, not to be bought again.
    """
    segment_id, segment, retail_id = await _price_lists(db, user)
    rates = await _rates_if_needed(products)
    sales = await live_flash_sales(
        db, (v.id for p in products for v in p.variants), datetime.now(timezone.utc)
    )
    brands = await _brand_terms(db)

    views = []
    for product in products:
        variants = []
        for v in sorted(
            (v for v in product.variants if v.is_active), key=lambda v: (v.position, v.sku)
        ):
            price, origin = _price_for(v, segment_id, rates)
            # The central pool: the same stock for every buyer, whatever their price list.
            available = stock_on_hand(v.inventory)
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
                    # The count for a firm's account only (`shows_stock_count`).
                    available_quantity=available if user and shows_stock_count(segment) else None,
                )
            )
        view = ProductRead.model_validate(product).model_dump()
        # The card's line over the name says the brand: the one its name carries where the field is empty.
        if not (view.get("brand") or "").strip():
            view["brand"] = brand_in_name(product.name, brands)
        # The labels the live variants carry, so the picker and the pills agree
        # even if a colour was retired after the product's own list was written.
        view["color_names"] = [v.color_name for v in variants if v.color_name]
        # In the caller's language, saying which languages it has.
        views.append(
            CatalogProductRead(
                **localize.product_view(view, product.translations, lang),
                variants=variants,
                segment_havale=segment in HAVALE_SWITCH and bool(getattr(product, HAVALE_SWITCH[segment])),
            )
        )
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
    brand: str | None = Query(None, max_length=100, description="Brand slug: that brand's products"),
    q: str | None = Query(None, max_length=100, description="Search name, brand or code"),
    ids: list[uuid.UUID] | None = Query(None, max_length=100, description="Only these products"),
    sort: Literal["name", "newest"] = Query("name", description="By name, or the newest first"),
    lang: Locale | None = LANG,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Catalog page, priced for the caller (see _catalog_view).

    `ids` narrows it to a set the page already chose (the product page's
    "Benzer Ürünler", re-priced for a signed-in viewer). `sort=newest` is the
    homepage's "Yeni Gelenler": the latest added first — the imported catalog
    shares one creation time, so among those it falls back on the name."""
    stmt = apply_catalog_filters(select(Product), category=category, q=q, published_only=True, lang=lang, brand=brand)
    if ids:
        stmt = stmt.where(Product.id.in_(ids))
    order = (Product.name, Product.id)
    # In the catalog's own order ("Önerilen"), what nobody can buy goes last: a
    # colour of it in the central stock or not. "Yeni Gelenler" stays newest first.
    buyable = _buyable()
    if sort == "newest":
        order = (Product.created_at.desc(), *order)
        buyable = None
    elif brand and not category:
        # A brand's page: its nargile sets first, then the rest of what it makes.
        sets = Product.id.in_(products_in(category_subtree(SETS_CATEGORY)))
        order = (sets.desc(), *order)
    elif category:
        # A category page in its own order ("Önerilen"): the products the
        # admin ranked for this very category first, the rest by name.
        rank = (
            select(CategoryProductOrder.position)
            .where(
                CategoryProductOrder.category_id == select(Category.id).where(Category.slug == category).scalar_subquery(),
                CategoryProductOrder.product_id == Product.id,
            )
            .correlate(Product)
            .scalar_subquery()
        )
        order = (rank.asc().nulls_last(), *order)
    result = await db.execute(
        stmt.options(*_VARIANT_LOADS).order_by(*((buyable.desc(),) if buyable is not None else ()), *order).offset(skip).limit(limit)
    )
    return await _catalog_view(db, user, list(result.scalars().all()), lang)


def _buyable():
    """Whether a product has a live colour in the central stock, for ordering by
    (sold out last). The same for every caller: there is one stock."""
    return (
        select(ProductVariant.id)
        .join(Inventory, Inventory.variant_id == ProductVariant.id)
        .where(
            ProductVariant.product_id == Product.id,
            ProductVariant.is_active.is_(True),
            Inventory.warehouse_type == CENTRAL_STOCK,
            Inventory.quantity > 0,
        )
        .correlate(Product)
        .exists()
    )


async def _live_variants(db: AsyncSession, ids) -> list[ProductVariant]:
    """These colours, where they are still for sale (live, of a published product), with their stock."""
    return list(
        (
            await db.execute(
                select(ProductVariant)
                .join(Product, Product.id == ProductVariant.product_id)
                .where(ProductVariant.id.in_(ids), ProductVariant.is_active.is_(True), Product.is_published.is_(True))
                .options(selectinload(ProductVariant.inventory), selectinload(ProductVariant.product))
            )
        ).scalars()
    )


@router.get("/summary", response_model=CatalogSummary)
async def catalog_summary(
    category: str | None = Query(None, max_length=180, description="Category slug"),
    brand: str | None = Query(None, max_length=100, description="Brand slug: that brand's products"),
    q: str | None = Query(None, max_length=100, description="Search name, brand or code"),
    lang: Locale | None = LANG,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """A listing as a whole, where its page shows it a page at a time: how
    many products it has, and the cheapest and dearest of them as their cards
    price them for the caller — the colour a card shows (the first in the
    caller's stock, else the first), at a live flash sale's price, else the
    caller's own, else the retail one; in TRY."""
    products = (
        await db.execute(
            apply_catalog_filters(select(Product), category=category, q=q, published_only=True, lang=lang, brand=brand).options(
                *_VARIANT_LOADS
            )
        )
    ).scalars().all()
    segment_id, segment, retail_id = await _price_lists(db, user)
    rates = await _rates_if_needed(list(products))
    sales = await live_flash_sales(db, (v.id for p in products for v in p.variants), datetime.now(timezone.utc))
    prices: list[Decimal] = []
    for product in products:
        live = sorted((v for v in product.variants if v.is_active), key=lambda v: (v.position, v.sku))
        if not live:
            continue
        shown = next((v for v in live if stock_on_hand(v.inventory) > 0), live[0])
        own, _ = _price_for(shown, segment_id, rates)
        sale = _flash_sale_for(own, sales.get(shown.id))
        price = sale.price if sale else (own or _price_for(shown, retail_id, rates)[0])
        if price is not None and price.currency == CurrencyType.TRY:
            prices.append(price.price_incl_tax)
    return CatalogSummary(
        total=len(products),
        price_min=min(prices) if prices else None,
        price_max=max(prices) if prices else None,
    )


@router.get("/cart-lines", response_model=list[CartLineInfo])
async def cart_lines(
    ids: list[uuid.UUID] = Query(..., max_length=100, description="Variant ids in the cart"),
    db: AsyncSession = Depends(get_db),
):
    """The cart's lines as the cart page shows them before a quote: each live
    colour of a published product with its page, its code and whether the
    central stock has any left. A colour no longer for sale is left out. How
    many are left is not said (`stock_check` holds a quantity to it)."""
    return [
        CartLineInfo(
            variant_id=v.id,
            product_id=v.product_id,
            product_slug=v.product.slug,
            sku=v.sku,
            in_stock=stock_on_hand(v.inventory) > 0,
        )
        for v in await _live_variants(db, ids)
    ]


@router.post("/stock-check", response_model=list[StockCheckLine])
async def stock_check(
    payload: StockCheckRequest,
    db: AsyncSession = Depends(get_db),
):
    """Quantities held to the central stock — the same for every caller — so
    the cart and the product page can take a quantity down to the most
    there is the moment it is typed — "Stok sınırına ulaştınız" — without
    being told the count: each line comes back as asked, or as the most there
    is when that is less. A colour no longer for sale comes back as 0. Checkout
    holds the order to the same stock under a lock."""
    have = {v.id: stock_on_hand(v.inventory) for v in await _live_variants(db, [i.variant_id for i in payload.items])}
    out = []
    for item in payload.items:
        allowed = min(item.quantity, have.get(item.variant_id, 0))
        out.append(StockCheckLine(variant_id=item.variant_id, quantity=allowed, capped=allowed < item.quantity))
    return out


@router.get("/suggest", response_model=SearchSuggestions)
async def suggest(
    q: str = Query(..., min_length=2, max_length=100),
    lang: Locale | None = LANG,
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """The header search's suggestions as a visitor types (from the second
    letter): the first few products the full search would list, priced for
    the caller, the categories whose name matches, and how many products the
    full search finds. Matched as the search matches (`apply_catalog_filters`:
    Turkish letters folded, so "lule" suggests the Lüleler)."""
    found = apply_catalog_filters(select(Product.id), category=None, q=q, published_only=True, lang=lang)
    total = await db.scalar(select(func.count()).select_from(found.subquery()))
    buyable = _buyable()
    products = (
        await db.execute(
            apply_catalog_filters(select(Product), category=None, q=q, published_only=True, lang=lang)
            .options(*_VARIANT_LOADS)
            .order_by(buyable.desc(), Product.name, Product.id)
            .limit(SUGGESTED_PRODUCTS)
        )
    ).scalars().all()
    pattern = f"%{_escape_like(_fold_tr(q.strip()))}%"
    categories = (
        await db.execute(
            select(Category)
            .where(Category.is_active.is_(True), _folded(Category.name).like(pattern, escape="\\"))
            .order_by(Category.sort_order, Category.name)
            .limit(SUGGESTED_CATEGORIES)
        )
    ).scalars().all()
    return SearchSuggestions(
        total=total or 0,
        products=await _catalog_view(db, user, list(products), lang),
        categories=[
            # In the page's language where the category has been translated.
            SuggestedCategory(name=(((c.translations or {}).get(lang) or {}).get("name") or c.name), slug=c.slug)
            for c in categories
        ],
    )


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
    order given (each once), with each group's regular list price (no flash
    sales: a catalogue outlives a sale). A group the product is not sold to is
    left out of its prices. Who may ask for which group is the caller's to
    decide.

    A business group's price (Bayi, Toptan, Kafe) is given as it was entered:
    one priced in USD or EUR stays in USD or EUR, as that buyer is quoted it.
    Retail is always in TRY — a shopper's price — at the day's TCMB rate, and
    left out when that rate cannot be had."""
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
                price, entered = _price_for(variant, segments[group], rates)
                if entered is not None and group != SegmentType.RETAIL:
                    prices[group] = entered  # the foreign figure, as entered
                elif price is not None and price.currency == CurrencyType.TRY:
                    prices[group] = price
        items.append(PriceListItem(
            product_id=product.id, code=product.base_code, name=product.name,
            image_url=product.image_url, prices=prices,
        ))
    return items


@router.get("/flash-sales", response_model=list[CatalogProductRead])
async def list_flash_sale_products(
    limit: int = Query(12, ge=1, le=50),
    lang: Locale | None = LANG,
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
    return await _catalog_view(db, user, list(result.scalars().all()), lang)


# Orders that count as sold: approved (paid), shipped or delivered. Pending,
# cancelled, failed and returned ones are not, and a quote (an export request) moves no
# stock — it is not a sale.
SOLD_STATUSES = PAID_STATUSES


@router.get("/bestsellers", response_model=list[CatalogProductRead])
async def list_bestsellers(
    limit: int = Query(10, ge=1, le=50),
    lang: Locale | None = LANG,
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
    return await _catalog_view(db, user, list(result.scalars().all()), lang)


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
async def list_sultanpuan_products(lang: Locale | None = LANG, db: AsyncSession = Depends(get_db)):
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
        for product in await _catalog_view(db, None, list(result.scalars().all()), lang)
        if (price := _current_retail_price(product)) is not None and loyalty.band_min <= price <= loyalty.band_max
    ]
    showcase.products = [product for _, product in sorted(priced, key=lambda pair: pair[0])]
    return showcase


@router.get("/{product_ref}", response_model=CatalogProductDetail)
async def get_product(
    product_ref: str = Path(max_length=255, description="The product's slug, or its id"),
    lang: Locale | None = LANG,
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
    views = await _catalog_view(db, user, [product, *pinned], lang)
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
