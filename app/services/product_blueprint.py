"""Reads and writes the admin's product sheet across its five tables.

The sheet is one screen but five tables: products (info, the rule flags, the
"Açıklama ve SEO" block, the Fırsat block), product_variants (one row per
colour), inventory (four quotas per colour), variant_prices (the currency ×
segment matrix) and flash_sales (the live sale price). Everything here is
written in the caller's transaction, so a save is all or nothing.

Colours are the variant axis: the sheet's colour pills become one variant each,
and each variant owns its own four quotas. What the sheet holds once per product
— the price matrix, the weight, the Fırsat price — is written to *every* colour,
because the tables that carry it hang off the variant. That fan-out is what
keeps one screen honest about a schema that can price colours apart later.

flash_sales is derived, never edited by hand from here on: the Fırsat block on
the product is the input, and `_sync_flash_sale` makes the sale rows match it.
That keeps one writer, so the storefront price and the sheet can't drift apart.
"""

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.catalog import (
    Category,
    CurrencyType,
    Inventory,
    Product,
    ProductCategory,
    ProductCrossSell,
    ProductVariant,
    VariantPrice,
    WarehouseType,
)
from app.models.promotion import FlashSale
from app.models.user import CustomerSegment, SegmentType
from app.services import slug_redirects
from app.services.pricing_engine import pick_price_row
from app.models.order import OrderItem
from app.schemas.product_sheet import (
    CrossSellCard,
    ProductSheetRead,
    ProductSheetWrite,
    SheetOpportunity,
    SheetPriceCell,
    SheetPriceRow,
    SheetRules,
    SheetStock,
    SheetVariant,
    fold,
)

# The sheet's stock boxes, in sheet order, and the quota each one fills.
STOCK_FIELDS: tuple[tuple[str, SegmentType, WarehouseType], ...] = (
    ("bayi", SegmentType.DEALER, WarehouseType.DEALER_STOCK),
    ("toptan", SegmentType.WHOLESALE, WarehouseType.B2B_STOCK),
    ("kafe", SegmentType.CAFE, WarehouseType.CAFE_STOCK),
    ("perakende", SegmentType.RETAIL, WarehouseType.RETAIL_STOCK),
)
RULE_FIELDS = (
    "is_free_shipping",
    "free_shipping_min_quantity",
    "has_havale_discount",
    "has_dealer_havale_discount",
    "has_toptan_havale_discount",
    "has_cafe_havale_discount",
    "has_volume_discount",
    "gives_points",
    "can_use_points",
    "is_cart_rule_applied",
)
OPPORTUNITY_FIELDS = (
    "is_opportunity",
    "opportunity_price",
    "opportunity_stock",
    "opportunity_end_date",
    "opportunity_color",
    "opp_is_free_shipping",
    "opp_free_shipping_min_quantity",
    "opp_has_havale_discount",
    "opp_has_volume_discount",
    "opp_gives_points",
    "opp_can_use_points",
)
PRODUCT_FIELDS = (
    "is_published",
    "base_code",
    "name",
    "brand",
    "category_id",
    "color_names",
    "description",
    "meta_title",
    "meta_description",
    "image_urls",
    "youtube_video_url",
)

SKU_MAX = 50
# A colour label as a stock code: Turkish letters folded to ASCII, the rest to
# dashes. Mirrored by the migration that first split the colours out.
_TR_ASCII = str.maketrans("ÇĞİIÖŞÜçğıiöşü", "CGIIOSUcgiiosu")


def color_slug(label: str) -> str:
    """"Açık Mavi" → "ACIK-MAVI"; "" when the label holds nothing codeable."""
    return re.sub(r"[^A-Z0-9]+", "-", label.translate(_TR_ASCII).upper()).strip("-")

_LOADS = (
    selectinload(Product.category_links),
    selectinload(Product.variants).selectinload(ProductVariant.prices),
    selectinload(Product.variants).selectinload(ProductVariant.inventory),
    # The pinned products, with enough of each to draw its preview card.
    selectinload(Product.cross_sells)
    .selectinload(ProductCrossSell.related)
    .selectinload(Product.variants)
    .selectinload(ProductVariant.prices),
)


class SheetError(ValueError):
    """A sheet the admin has to correct (the API turns it into a 422/409)."""


async def _segment_ids(db: AsyncSession) -> dict[SegmentType, int]:
    rows = (await db.execute(select(CustomerSegment))).scalars()
    return {SegmentType(s.code): s.id for s in rows}


async def load(db: AsyncSession, product_id: uuid.UUID) -> Product | None:
    """The product with everything the sheet needs.

    `populate_existing` because the session does not expire on commit: without it
    a save would read back its own stale collections, deleted price rows included.
    """
    return await db.scalar(
        select(Product)
        .where(Product.id == product_id)
        .options(*_LOADS)
        .execution_options(populate_existing=True)
    )


def sheet_variants(product: Product) -> list[ProductVariant]:
    """The product's live colours, in the admin's order (see `_LOADS`).

    Retired colours are left out: they exist for the orders that contain them,
    not for the sheet. A product from before colours had stock has one unnamed
    variant, which is exactly one entry here.
    """
    return sorted(
        (v for v in product.variants if v.is_active), key=lambda v: (v.position, v.sku)
    )


async def _fetch_variants(db: AsyncSession, product: Product) -> list[ProductVariant]:
    """Every variant of the product, retired ones included, as a query: on the
    write path the product may be brand new, whose `variants` would lazy-load
    mid-transaction. Retired rows are included because re-adding a colour has to
    find and revive its own row rather than collide with it."""
    return list(
        (
            await db.execute(
                select(ProductVariant)
                .where(ProductVariant.product_id == product.id)
                .order_by(ProductVariant.position, ProductVariant.sku)
            )
        ).scalars()
    )


async def save(
    db: AsyncSession, payload: ProductSheetWrite, product: Product | None = None
) -> Product:
    """Create or update a product from its sheet. Flushes; the caller commits."""
    segments = await _segment_ids(db)
    now = datetime.now(timezone.utc)
    # Before anything is written: the primary one goes on the product row itself.
    if payload.category_ids:
        found = set((await db.execute(select(Category.id).where(Category.id.in_(payload.category_ids)))).scalars())
        if any(cid not in found for cid in payload.category_ids):
            raise SheetError("Seçilen kategorilerden biri bulunamadı")

    if payload.slug and payload.slug != (product.slug if product else None):
        clash = await db.scalar(
            select(Product.id).where(Product.slug == payload.slug, Product.id != (product.id if product else None))
        )
        if clash is not None:
            raise SheetError(f"“{payload.slug}” adresi başka bir üründe kullanılıyor")

    if product is None:
        product = Product(tags=[], slug=payload.slug)
        db.add(product)
    elif payload.slug and payload.slug != product.slug:
        # Renamed: the old address keeps leading here (and to nowhere else).
        await slug_redirects.moved(db, "product", product.slug, payload.slug, product_id=product.id)
        product.slug = payload.slug
    for field in PRODUCT_FIELDS:
        setattr(product, field, getattr(payload, field))
    # The cover stays in `image_url`, where every one-photo reader looks.
    product.image_url = payload.image_urls[0] if payload.image_urls else None
    for field in RULE_FIELDS:
        setattr(product, field, getattr(payload.rules, field))
    for field in OPPORTUNITY_FIELDS:
        setattr(product, field, getattr(payload.opportunity, field))
    await db.flush()

    variants = await _sync_variants(db, product, payload)
    for variant, sheet in variants:
        # Only when the caller actually sent it: the product form does not edit the
        # weight, and an omitted field must leave the stored one alone.
        if "weight_kg" in payload.model_fields_set:
            variant.weight_kg = payload.weight_kg
        await _sync_stock(db, variant, sheet.stock)
        await _sync_prices(db, variant, payload.prices, segments)
        await _sync_flash_sale(db, variant, payload.opportunity, now)
    await _sync_cross_sells(db, product, payload.related_product_ids)
    category_ids = await _with_deals_category(db, payload.category_ids or [], product.is_opportunity)
    if product.category_id not in category_ids:
        product.category_id = category_ids[0] if category_ids else None
    await _sync_categories(db, product, category_ids)

    await db.flush()
    return product


async def _with_deals_category(db: AsyncSession, category_ids: list[int], is_opportunity: bool) -> list[int]:
    """The product's categories with the deals category ("Fırsat Ürünler",
    DEALS_CATEGORY_SLUG) in step with its "Fırsat Ürünü" switch: added at the end
    while it is on, taken out when it is off. The others stay as they are, in
    their order; without such a category, nothing changes."""
    deals = await db.scalar(select(Category.id).where(Category.slug == settings.DEALS_CATEGORY_SLUG))
    if deals is None:
        return category_ids
    others = [cid for cid in category_ids if cid != deals]
    return [*others, deals] if is_opportunity else others


async def _sync_categories(db: AsyncSession, product: Product, category_ids: list[int]) -> None:
    """Replace the categories the product is listed under, primary first.

    `products.category_id` (the primary) is already set from the same list,
    which `save` has checked.
    """
    # Cleared and rewritten, as the cross-sells are: short, and positions stay contiguous.
    await db.execute(delete(ProductCategory).where(ProductCategory.product_id == product.id))
    db.add_all(
        ProductCategory(product_id=product.id, category_id=cid, position=i) for i, cid in enumerate(category_ids)
    )


async def _sync_variants(
    db: AsyncSession, product: Product, payload: ProductSheetWrite
) -> list[tuple[ProductVariant, SheetVariant]]:
    """Make the product's variants match the sheet's colours, and pair them up.

    Each sheet entry is matched to a stored row by id, then by colour label — so
    a colour the admin removed and added back finds its own row again, retired or
    not, instead of colliding with it. A colour that has gone is deleted outright
    when nothing was ever ordered from it, and retired (`is_active = False`, every
    quota zeroed) when something was: an order line has to keep naming the thing
    it sold.

    When *nothing* in the sheet matches and the product has exactly one variant,
    that variant becomes the sheet's first entry rather than being replaced. That
    is the only reading of "this product now has colours" (or has stopped having
    them, or had its one colour relabelled) that does not throw away the stock the
    product is holding — the same choice the migration made when it split the
    colours out. With two or more stored variants there is no such single answer,
    so the unmatched ones go the usual way.
    """
    stored = await _fetch_variants(db, product)
    by_id = {v.id: v for v in stored}
    by_color = {fold(v.color_name): v for v in stored if v.color_name}

    # Pass one: which stored row is each sheet entry?
    matched: list[ProductVariant | None] = []
    claimed: set[uuid.UUID] = set()
    for sheet in payload.variants:
        variant = by_id.get(sheet.id) if sheet.id else None
        if variant is None and sheet.color:
            variant = by_color.get(fold(sheet.color))
        if variant is None and sheet.color is None:
            # A colourless product's lone row may predate colours and have no label.
            variant = next((v for v in stored if not v.color_name), None)
        if variant is not None and variant.id in claimed:
            variant = None  # an earlier entry already took it
        if variant is not None:
            claimed.add(variant.id)
        matched.append(variant)

    if matched and not claimed and len(stored) == 1:
        matched[0] = stored[0]
        claimed.add(stored[0].id)

    base = (payload.sku or payload.base_code).strip()
    # Codes other products own: deriving one of those is the admin's mistake, not
    # something to quietly number around.
    foreign = await _taken_skus(db, exclude=[v.id for v in stored])
    mine: set[str] = set()

    # Pass two: what each row should end up being.
    targets: list[tuple[ProductVariant | None, str, SheetVariant]] = []
    for sheet, variant in zip(payload.variants, matched):
        sku = _variant_sku(base, sheet.color, foreign=foreign, mine=mine)
        mine.add(sku)
        targets.append((variant, sku, sheet))

    await _park_swapped(db, targets)

    paired: list[tuple[ProductVariant, SheetVariant]] = []
    for position, (variant, sku, sheet) in enumerate(targets):
        if variant is None:
            variant = ProductVariant(product_id=product.id, sku=sku)
            db.add(variant)
            await db.flush()
        else:
            variant.sku = sku
        variant.color_name = sheet.color
        variant.position = position
        variant.is_active = True
        paired.append((variant, sheet))

    for variant in stored:
        if variant.id not in claimed:
            await _retire_variant(db, variant)
    return paired


async def _park_swapped(
    db: AsyncSession, targets: list[tuple[ProductVariant | None, str, SheetVariant]]
) -> None:
    """Move a code or label out of the way when another row of the same product is
    about to take it.

    Swapping two colours' labels (or their codes, which follow the labels) would
    otherwise double a value for as long as one UPDATE: the unique SKU index and
    the (product, colour) pair are both checked per row, not at commit, so the
    flush would fail. Parking the rows on throwaway values first makes the swap
    two steps, which is what it has to be. Nothing is parked when nothing moves,
    so the usual save pays nothing for this.
    """
    wanted_skus = {sku for variant, sku, _ in targets if variant is None or variant.sku != sku}
    wanted_colors = {
        fold(sheet.color)
        for variant, _, sheet in targets
        if sheet.color and (variant is None or variant.color_name != sheet.color)
    }
    parked = [
        variant
        for variant, sku, sheet in targets
        if variant is not None
        and (
            (variant.sku != sku and variant.sku in wanted_skus)
            or (
                variant.color_name
                and variant.color_name != sheet.color
                and fold(variant.color_name) in wanted_colors
            )
        )
    ]
    if not parked:
        return
    for variant in parked:
        # Unique by construction, and not a code anything else could be called.
        variant.sku = f"__{variant.id.hex}"[:SKU_MAX]
        variant.color_name = None  # several NULLs are distinct under the constraint
    await db.flush()


async def _taken_skus(db: AsyncSession, exclude: list[uuid.UUID]) -> set[str]:
    """Every SKU in use outside this product, so a derived one cannot shadow one."""
    stmt = select(ProductVariant.sku)
    if exclude:
        stmt = stmt.where(ProductVariant.id.not_in(exclude))
    return set((await db.execute(stmt)).scalars())


def _variant_sku(base: str, color: str | None, *, foreign: set[str], mine: set[str]) -> str:
    """The stock code for one colour: "<ürün kodu>-<RENK>", or the bare code when
    the product has no colours (which is what it has always been).

    A code another product owns is refused outright — that is a duplicate product
    code, and the admin has to see it. A clash between two of *this* product's own
    colours (two labels that spell the same code, or a label with nothing codeable
    in it) is numbered around instead, because neither label is wrong.
    """
    slug = color_slug(color) if color else ""
    tail = f"-{slug}" if slug else ""
    candidate = f"{base[: SKU_MAX - len(tail)]}{tail}"
    if candidate in foreign:
        raise SheetError(f"“{candidate}” kodu başka bir ürüne ait")
    if candidate not in mine:
        return candidate

    stem = slug or "RENK"
    for attempt in range(2, 100):
        tail = f"-{stem}-{attempt}"
        numbered = f"{base[: SKU_MAX - len(tail)]}{tail}"
        if numbered not in foreign and numbered not in mine:
            return numbered
    raise SheetError(f"“{base}” için boş bir stok kodu bulunamadı")


async def _retire_variant(db: AsyncSession, variant: ProductVariant) -> None:
    """Drop a colour the sheet no longer lists, or shelve it if it has been sold."""
    sold = (
        await db.scalar(select(OrderItem.id).where(OrderItem.variant_id == variant.id).limit(1))
    ) is not None
    if not sold:
        await db.execute(delete(FlashSale).where(FlashSale.variant_id == variant.id))
        for table in (VariantPrice, Inventory):
            await db.execute(delete(table).where(table.variant_id == variant.id))
        await db.delete(variant)
        return
    # Sold: keep the row for its order lines, but let nothing else reach it.
    variant.is_active = False
    sale = await db.scalar(select(FlashSale).where(FlashSale.variant_id == variant.id))
    if sale is not None:
        sale.is_active = False
    for row in (await db.execute(select(Inventory).where(Inventory.variant_id == variant.id))).scalars():
        row.quantity = 0


async def _sync_stock(db: AsyncSession, variant: ProductVariant, stock: SheetStock) -> None:
    """One inventory row per quota, created on first use and never deleted (a zero
    quota is meaningful: "sold out for this segment")."""
    rows = {
        i.warehouse_type: i
        for i in (await db.execute(select(Inventory).where(Inventory.variant_id == variant.id))).scalars()
    }
    for field, _segment, warehouse in STOCK_FIELDS:
        quantity = getattr(stock, field)
        row = rows.get(warehouse)
        if row is None:
            db.add(Inventory(variant_id=variant.id, warehouse_type=warehouse, quantity=quantity))
        else:
            row.quantity = quantity


async def _sync_prices(
    db: AsyncSession,
    variant: ProductVariant,
    rows: list[SheetPriceRow],
    segments: dict[SegmentType, int],
) -> None:
    """Upsert the filled cells of the matrix and drop the ones the admin cleared."""
    existing = {
        (CurrencyType(p.currency), p.segment_id): p
        for p in (await db.execute(select(VariantPrice).where(VariantPrice.variant_id == variant.id))).scalars()
    }
    seen: set[tuple[CurrencyType, int]] = set()

    for row in rows:
        for segment, cell in row.cells.items():
            segment_id = segments.get(segment)
            if segment_id is None:
                raise SheetError(f"Bilinmeyen müşteri grubu: {segment}")
            key = (CurrencyType(row.currency), segment_id)
            if not cell.filled:
                continue
            excl, incl, vat = cell.resolved()
            price = existing.get(key)
            if price is None:
                db.add(
                    VariantPrice(
                        variant_id=variant.id,
                        segment_id=segment_id,
                        currency=row.currency,
                        price_excl_tax=excl,
                        price_incl_tax=incl,
                        vat_rate=vat,
                    )
                )
            else:
                price.price_excl_tax, price.price_incl_tax, price.vat_rate = excl, incl, vat
            seen.add(key)

    # A cell the admin emptied stops being a price at all.
    submitted_currencies = {CurrencyType(r.currency) for r in rows}
    for key, price in existing.items():
        if key[0] in submitted_currencies and key not in seen:
            await db.delete(price)


async def _sync_flash_sale(
    db: AsyncSession, variant: ProductVariant, opportunity: SheetOpportunity, now: datetime
) -> None:
    """Make the variant's sale row match the sheet's Fırsat block.

    An opportunity that is off (or has no price) leaves no sale row active. A
    deadline is optional: without one the row has no `ends_at` and the offer runs
    until its stock runs out. Whether it is still claimable is decided at read
    time by `live_flash_sales`, which also checks the stock and the switch.
    """
    sale = await db.scalar(select(FlashSale).where(FlashSale.variant_id == variant.id))
    priced = opportunity.is_opportunity and opportunity.opportunity_price is not None
    if not priced:
        if sale is not None:
            sale.is_active = False
        return

    ends_at = opportunity.opportunity_end_date
    if ends_at is not None:
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        if ends_at <= now:
            raise SheetError("Fırsat bitiş tarihi gelecekte olmalı")

    if sale is None:
        db.add(
            FlashSale(
                variant_id=variant.id,
                sale_price_incl_tax=opportunity.opportunity_price,
                starts_at=now,
                ends_at=ends_at,
                is_active=True,
            )
        )
        return
    sale.sale_price_incl_tax = opportunity.opportunity_price  # type: ignore[assignment]
    # Keep a running sale's start so its countdown doesn't restart on every save.
    sale.starts_at = min(sale.starts_at, now) if sale.is_active else now
    sale.ends_at = ends_at
    sale.is_active = True


async def _sync_cross_sells(db: AsyncSession, product: Product, related_ids: list[uuid.UUID]) -> None:
    """Replace the product's "birlikte satılanlar" with the admin's list, in order."""
    if product.id in related_ids:
        raise SheetError("Ürün kendisiyle birlikte satılamaz")
    if related_ids:
        found = set(
            (await db.execute(select(Product.id).where(Product.id.in_(related_ids)))).scalars()
        )
        missing = [pid for pid in related_ids if pid not in found]
        if missing:
            raise SheetError("Seçilen birlikte satılan ürünlerden biri bulunamadı")

    # The list is short and fully replaced on every save: clearing is simpler than
    # diffing, and keeps the stored positions contiguous.
    await db.execute(delete(ProductCrossSell).where(ProductCrossSell.product_id == product.id))
    for position, related_id in enumerate(related_ids):
        db.add(ProductCrossSell(product_id=product.id, related_product_id=related_id, position=position))


def cross_sell_cards(product: Product, retail_segment_id: int | None) -> list[CrossSellCard]:
    """The pinned products as the form's preview cards, in the admin's order.

    `product.cross_sells` must be loaded with its `related` products and their
    prices (see `_LOADS`).
    """
    cards = []
    for link in sorted(product.cross_sells, key=lambda c: c.position):
        related = link.related
        if related is None:  # the product was deleted (the cascade removes the row)
            continue
        price = None
        for variant in related.variants:
            price = pick_price_row(variant.prices, retail_segment_id) if retail_segment_id else None
            if price is not None:
                break
        cards.append(
            CrossSellCard(
                id=related.id,
                base_code=related.base_code,
                name=related.name,
                image_url=related.image_url,
                price_incl_tax=price.price_incl_tax if price else None,
                currency=CurrencyType(price.currency) if price else None,
            )
        )
    return cards


async def duplicate(db: AsyncSession, product: Product, segments: dict[int, SegmentType]) -> Product:
    """Deep-copy a product as an unpublished draft.

    The copy is written through `save`, from the source's own sheet, so it carries
    everything the sheet carries — quotas, the whole price matrix, the rules, the
    Fırsat block with its sale row, the gallery and the cross-sells — and cannot
    drift from what saving the form would have produced.
    """
    sheet = read(product, segments)
    payload = ProductSheetWrite(
        **sheet.model_dump(
            exclude={"id", "variant_id", "sku", "stock_total", "related_products", "weight_kg", "variants"}
        ),
        # The copy's colours are its own rows, so they carry the source's stock but
        # none of its ids or codes — those would point the save back at the source.
        variants=[SheetVariant(color=v.color, stock=v.stock) for v in sheet.variants],
        weight_kg=sheet.weight_kg,
    )
    # One free code serves as both the product code and the stem its colours'
    # stock codes are derived from, so `sku` is left for `save` to derive.
    payload.base_code = await _free_code(db, sheet.base_code)
    payload.sku = None
    payload.name = f"{sheet.name} (Kopya)"[:255]
    payload.slug = None  # the copy's own, from its name
    payload.is_published = False  # a draft until the admin says otherwise
    return await save(db, payload)


# A product code is 30 characters; its colours' stock codes add "-<RENK>" on top.
BASE_CODE_MAX = 30


async def _free_code(db: AsyncSession, code: str) -> str:
    """"<code>-KOPYA", then -KOPYA2, -KOPYA3… until one no product and no stock
    code has taken. Both pools, because the copy's code becomes both."""
    taken = set((await db.execute(select(Product.base_code))).scalars())
    taken |= set((await db.execute(select(ProductVariant.sku))).scalars())
    for n in range(1, 100):
        suffix = f"-KOPYA{'' if n == 1 else n}"
        candidate = f"{code[: BASE_CODE_MAX - len(suffix)]}{suffix}"
        if candidate not in taken:
            return candidate
    raise SheetError("Kopya için boş bir ürün kodu bulunamadı")


async def delete_product(db: AsyncSession, product: Product) -> None:
    """Remove a product and everything that only exists for it.

    Order lines are *not* touched: a product that has been sold cannot be deleted
    (the caller checks `is_sold` first), because its lines are part of the orders.
    """
    variant_ids = [v.id for v in product.variants]
    if variant_ids:
        for table in (VariantPrice, Inventory):
            await db.execute(delete(table).where(table.variant_id.in_(variant_ids)))
        # flash_sales rows go with the variant (ON DELETE CASCADE).
        await db.execute(delete(ProductVariant).where(ProductVariant.id.in_(variant_ids)))
    # product_cross_sells rows go with the product, in both directions.
    await db.delete(product)


async def is_sold(db: AsyncSession, product: Product) -> bool:
    """Whether any of the product's variants appears in an order."""
    if not product.variants:
        return False
    return (
        await db.scalar(
            select(OrderItem.id).where(OrderItem.variant_id.in_([v.id for v in product.variants])).limit(1)
        )
    ) is not None


def _variant_stock(variant: ProductVariant) -> SheetStock:
    rows = {i.warehouse_type: i.quantity for i in variant.inventory}
    return SheetStock(**{field: rows.get(warehouse, 0) for field, _s, warehouse in STOCK_FIELDS})


def read(product: Product, segments: dict[int, SegmentType]) -> ProductSheetRead:
    """The saved sheet, exactly as the form should reload it."""
    live = sheet_variants(product)
    variant = live[0] if live else None
    sheet_colours = [
        SheetVariant(id=v.id, color=v.color_name, sku=v.sku, stock=_variant_stock(v)) for v in live
    ]
    # The form's "Toplam Stok": every quota of every colour.
    stock = SheetStock()
    for colour in sheet_colours:
        stock = stock + colour.stock

    by_currency: dict[CurrencyType, dict[SegmentType, SheetPriceCell]] = {c: {} for c in CurrencyType}
    for price in variant.prices if variant else []:
        segment = segments.get(price.segment_id)
        if segment is None:
            continue
        by_currency[CurrencyType(price.currency)][segment] = SheetPriceCell(
            price_excl_tax=price.price_excl_tax,
            price_incl_tax=price.price_incl_tax,
            vat_rate=price.vat_rate,
        )

    retail_id = next((sid for sid, code in segments.items() if code == SegmentType.RETAIL), None)
    fields = {f: getattr(product, f) for f in PRODUCT_FIELDS}
    # The colours the variants actually are, which is what the stock blocks are
    # keyed on — a stored label the product's own list has lost would otherwise
    # drop a colour (and its stock) out of the form.
    fields["color_names"] = [v.color for v in sheet_colours if v.color]
    # A product saved before it could have several still has its primary one.
    category_ids = [link.category_id for link in product.category_links]
    if product.category_id is not None and product.category_id not in category_ids:
        category_ids.insert(0, product.category_id)
    fields["category_ids"] = category_ids
    fields["slug"] = product.slug
    return ProductSheetRead(
        id=product.id,
        related_product_ids=[c.related_product_id for c in sorted(product.cross_sells, key=lambda c: c.position)],
        related_products=cross_sell_cards(product, retail_id),
        variant_id=variant.id if variant else uuid.UUID(int=0),
        sku=product.base_code,
        weight_kg=variant.weight_kg if variant else None,
        stock=stock,
        variants=sheet_colours,
        stock_total=stock.total,
        prices=[SheetPriceRow(currency=c, cells=cells) for c, cells in by_currency.items()],
        rules=SheetRules(**{f: getattr(product, f) for f in RULE_FIELDS}),
        opportunity=SheetOpportunity(**{f: getattr(product, f) for f in OPPORTUNITY_FIELDS}),
        **fields,
    )
