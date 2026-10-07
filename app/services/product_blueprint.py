"""Reads and writes the admin's product sheet across its five tables.

The sheet is one screen but five tables: products (info, colour, rule flags, the
Fırsat block), product_variants (the code), inventory (four quotas),
variant_prices (the currency × segment matrix) and flash_sales (the live sale
price). Everything here is written in the caller's transaction, so a save is all
or nothing.

flash_sales is derived, never edited by hand from here on: the Fırsat block on
the product is the input, and `_sync_flash_sale` makes the sale row match it.
That keeps one writer, so the storefront price and the sheet can't drift apart.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.catalog import (
    CurrencyType,
    Inventory,
    Product,
    ProductCrossSell,
    ProductVariant,
    VariantPrice,
    WarehouseType,
)
from app.models.promotion import FlashSale
from app.models.user import CustomerSegment, SegmentType
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
    "image_urls",
    "youtube_video_url",
)

_LOADS = (
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


def sheet_variant(product: Product) -> ProductVariant | None:
    """The variant the sheet edits, from an already-loaded product (see `_LOADS`).

    One product, one variant; the first by SKU wins if a product from before the
    sheet has several.
    """
    return min(product.variants, key=lambda v: v.sku) if product.variants else None


async def _fetch_sheet_variant(db: AsyncSession, product: Product) -> ProductVariant | None:
    """Same choice as `sheet_variant`, but as a query: on the write path the
    product may be brand new, whose `variants` would lazy-load mid-transaction."""
    return await db.scalar(
        select(ProductVariant)
        .where(ProductVariant.product_id == product.id)
        .order_by(ProductVariant.sku)
        .limit(1)
    )


async def save(
    db: AsyncSession, payload: ProductSheetWrite, product: Product | None = None
) -> Product:
    """Create or update a product from its sheet. Flushes; the caller commits."""
    segments = await _segment_ids(db)
    now = datetime.now(timezone.utc)

    if product is None:
        product = Product(tags=[])
        db.add(product)
    for field in PRODUCT_FIELDS:
        setattr(product, field, getattr(payload, field))
    # The cover stays in `image_url`, where every one-photo reader looks.
    product.image_url = payload.image_urls[0] if payload.image_urls else None
    for field in RULE_FIELDS:
        setattr(product, field, getattr(payload.rules, field))
    for field in OPPORTUNITY_FIELDS:
        setattr(product, field, getattr(payload.opportunity, field))
    await db.flush()

    variant = await _fetch_sheet_variant(db, product)
    sku = (payload.sku or payload.base_code).strip()
    if variant is None:
        clash = await db.scalar(select(ProductVariant.id).where(ProductVariant.sku == sku))
        if clash is not None:
            raise SheetError(f"“{sku}” kodu başka bir ürüne ait")
        variant = ProductVariant(product_id=product.id, sku=sku)
        db.add(variant)
        await db.flush()
    elif variant.sku != sku:
        clash = await db.scalar(
            select(ProductVariant.id).where(ProductVariant.sku == sku, ProductVariant.id != variant.id)
        )
        if clash is not None:
            raise SheetError(f"“{sku}” kodu başka bir ürüne ait")
        variant.sku = sku
    # Only when the caller actually sent it: the product form does not edit the
    # weight, and an omitted field must leave the stored one alone.
    if "weight_kg" in payload.model_fields_set:
        variant.weight_kg = payload.weight_kg

    await _sync_stock(db, variant, payload.stock)
    await _sync_prices(db, variant, payload.prices, segments)
    await _sync_flash_sale(db, variant, payload.opportunity, now)
    await _sync_cross_sells(db, product, payload.related_product_ids)

    await db.flush()
    return product


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
        **sheet.model_dump(exclude={"id", "variant_id", "sku", "stock_total", "related_products", "weight_kg"}),
        sku=await _free_code(db, select(ProductVariant.sku), sheet.sku, 50),
        weight_kg=sheet.weight_kg,
    )
    payload.base_code = await _free_code(db, select(Product.base_code), sheet.base_code, 30)
    payload.name = f"{sheet.name} (Kopya)"[:255]
    payload.is_published = False  # a draft until the admin says otherwise
    return await save(db, payload)


async def _free_code(db: AsyncSession, existing, code: str, limit: int) -> str:
    """"<code>-KOPYA", then -KOPYA2, -KOPYA3… until one is free within `limit`."""
    taken = set((await db.execute(existing)).scalars())
    for n in range(1, 100):
        suffix = f"-KOPYA{'' if n == 1 else n}"
        candidate = f"{code[: limit - len(suffix)]}{suffix}"
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


def read(product: Product, segments: dict[int, SegmentType]) -> ProductSheetRead:
    """The saved sheet, exactly as the form should reload it."""
    variant = sheet_variant(product)
    stock_rows = {i.warehouse_type: i.quantity for i in (variant.inventory if variant else [])}
    stock = SheetStock(**{field: stock_rows.get(warehouse, 0) for field, _s, warehouse in STOCK_FIELDS})

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
    return ProductSheetRead(
        id=product.id,
        related_product_ids=[c.related_product_id for c in sorted(product.cross_sells, key=lambda c: c.position)],
        related_products=cross_sell_cards(product, retail_id),
        variant_id=variant.id if variant else uuid.UUID(int=0),
        sku=variant.sku if variant else product.base_code,
        weight_kg=variant.weight_kg if variant else None,
        stock=stock,
        stock_total=stock.total,
        prices=[SheetPriceRow(currency=c, cells=cells) for c, cells in by_currency.items()],
        rules=SheetRules(**{f: getattr(product, f) for f in RULE_FIELDS}),
        opportunity=SheetOpportunity(**{f: getattr(product, f) for f in OPPORTUNITY_FIELDS}),
        **{f: getattr(product, f) for f in PRODUCT_FIELDS},
    )
