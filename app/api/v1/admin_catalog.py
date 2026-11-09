"""Admin-only catalog and inventory management endpoints."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_admin_user, get_session_factory
from app.core.database import get_db
from app.models.catalog import (
    Category,
    CurrencyType,
    Inventory,
    Product,
    ProductVariant,
    VariantPrice,
    WarehouseType,
)
from app.api.v1.products import apply_catalog_filters
from app.models.user import CustomerSegment, SegmentType, User
from app.schemas.admin import (
    AdminInventoryRead,
    BulkErpPricesResult,
    BulkErpPricesWrite,
    ProductDeleteResult,
    ProductIds,
    ProductSort,
    SortDirection,
    AdminProductDetail,
    AdminVariantDetail,
    ProductPublishRead,
    ProductPublishUpdate,
    ProductStockUpdate,
    AdminVariantPrice,
    SegmentRead,
)
from app.schemas.product_sheet import ProductSheetRead, ProductSheetWrite, SheetStock
from app.services import bulk_erp, product_blueprint
from app.services.checkout_engine import CENTRAL_STOCK
from app.services.exchange_rates import RatesUnavailable, get_rates
from app.services.pricing_engine import CURRENCY_PREFERENCE
from app.services import auto_translate

router = APIRouter(prefix="/admin", tags=["admin-catalog"])


# ── Translation drafts ("Çeviri Onayları") ───────────────────────────────────


@router.get("/translations/drafts")
async def translation_drafts(
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """What Claude has translated and nobody has approved yet: each row with
    its draft languages, products first. Their English and Arabic pages are
    shown but not indexed until approved in "Çeviriler"."""
    rows: list[dict] = []
    for kind, model in (("product", Product), ("category", Category)):
        for row in (await db.scalars(select(model).where(model.translations.isnot(None)).order_by(model.name))).all():
            drafts = [lang for lang, t in (row.translations or {}).items() if (t or {}).get("draft")]
            if drafts:
                rows.append({"kind": kind, "id": str(row.id), "name": row.name, "languages": sorted(drafts)})
    return rows


# ── The product sheet ("Ürün Ekle / Düzenle") ────────────────────────────────

@router.post("/products/sheet", response_model=ProductSheetRead, status_code=status.HTTP_201_CREATED)
async def create_product_sheet(
    payload: ProductSheetWrite,
    background: BackgroundTasks,
    sessions: async_sessionmaker = Depends(get_session_factory),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a product from the whole sheet: info, stock, price matrix,
    rule flags and the Fırsat block, in one transaction."""
    try:
        product = await product_blueprint.save(db, payload)
    except product_blueprint.SheetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await db.commit()
    # English and Arabic it lacks, written by Claude as drafts after the answer.
    background.add_task(auto_translate.translate, "product", product.id, sessions)
    return await _read_sheet(db, product.id)


@router.get("/products/{product_id}/sheet", response_model=ProductSheetRead)
async def read_product_sheet(
    product_id: uuid.UUID,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """The saved sheet, for the edit form."""
    return await _read_sheet(db, product_id)


@router.put("/products/{product_id}/sheet", response_model=ProductSheetRead)
async def update_product_sheet(
    product_id: uuid.UUID,
    payload: ProductSheetWrite,
    background: BackgroundTasks,
    sessions: async_sessionmaker = Depends(get_session_factory),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    product = await product_blueprint.load(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    try:
        await product_blueprint.save(db, payload, product)
    except product_blueprint.SheetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await db.commit()
    background.add_task(auto_translate.translate, "product", product_id, sessions)
    return await _read_sheet(db, product_id)


async def _read_sheet(db: AsyncSession, product_id: uuid.UUID) -> ProductSheetRead:
    product = await product_blueprint.load(db, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    segments = {s.id: SegmentType(s.code) for s in (await db.execute(select(CustomerSegment))).scalars()}
    return product_blueprint.read(product, segments)


async def _retail_segment_id(db: AsyncSession) -> int | None:
    return await db.scalar(select(CustomerSegment.id).where(CustomerSegment.code == SegmentType.RETAIL))


async def _product_order(db: AsyncSession, sort: ProductSort | None, direction: SortDirection):
    """ORDER BY for the admin products table, matching the column it sorts on.

    The stock and the price live a join away — spread across the product's colour
    variants and their retail price rows — so each is a scalar subquery per
    product rather than a join that would multiply the rows.
    """
    if sort is None:
        return (Product.name, Product.id)

    retail_stock = (
        select(func.coalesce(func.sum(Inventory.quantity), 0))
        .select_from(Inventory)
        .join(ProductVariant, ProductVariant.id == Inventory.variant_id)
        .where(
            ProductVariant.product_id == Product.id,
            ProductVariant.is_active.is_(True),
            Inventory.warehouse_type == CENTRAL_STOCK,
        )
        .correlate(Product)
        .scalar_subquery()
    )
    retail_id = await _retail_segment_id(db)

    def retail_in(currency: CurrencyType, try_per_unit: Decimal | None = None):
        """The product's cheapest retail price in `currency`, in TRY."""
        amount = VariantPrice.price_incl_tax if try_per_unit is None else VariantPrice.price_incl_tax * try_per_unit
        return (
            select(func.min(amount))
            .select_from(VariantPrice)
            .join(ProductVariant, ProductVariant.id == VariantPrice.variant_id)
            .where(
                ProductVariant.product_id == Product.id,
                ProductVariant.is_active.is_(True),
                VariantPrice.segment_id == retail_id,
                VariantPrice.currency == currency,
            )
            .correlate(Product)
            .scalar_subquery()
        )

    # The price the storefront charges: its TRY row, else its USD or EUR one at
    # the day's TCMB rate (pricing_engine's CURRENCY_PREFERENCE). Without rates
    # a foreign price can't be placed, and sorts with the priceless, last.
    retail_price = retail_in(CurrencyType.TRY)
    if sort == ProductSort.PRICE:
        try:
            rates = await get_rates()
        except RatesUnavailable:
            rates = None
        foreign = [
            retail_in(currency, rates.try_per_unit[currency.value])
            for currency in CURRENCY_PREFERENCE
            if currency != CurrencyType.TRY and rates and rates.try_per_unit.get(currency.value)
        ]
        retail_price = func.coalesce(retail_price, *foreign)

    column = {
        # The product code, not a stock code: a coloured product has one per colour.
        ProductSort.CODE: Product.base_code,
        ProductSort.NAME: Product.name,
        ProductSort.STOCK: retail_stock,
        ProductSort.PRICE: retail_price,
    }[sort]
    ordered = column.desc() if direction == SortDirection.DESC else column.asc()
    # A product with no price (or no variant) sorts last either way: "unknown" is
    # not "cheapest".
    return (ordered.nulls_last(), Product.name, Product.id)


@router.get("/products", response_model=list[AdminProductDetail])
async def list_products_admin(
    response: Response,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    q: str | None = Query(None, max_length=100),
    category: str | None = Query(None, max_length=180),
    order_by: ProductSort | None = Query(None, description="code | name | stock | price"),
    order_dir: SortDirection = Query(SortDirection.ASC),
    include_deleted: bool = Query(False, description="deleted products too"),
    pricing: bool = Query(False, description="each product's worksheet rule too (Fiyat Giriş Modu)"),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Products with every segment price and their stock, for the admin panel;
    deleted ones only when asked for. The X-Total-Count header carries the
    number of matches, for page controls."""
    filters = dict(category=category, q=q, include_deleted=include_deleted)
    total = await db.scalar(select(func.count()).select_from(apply_catalog_filters(select(Product.id), **filters).subquery()))
    response.headers["X-Total-Count"] = str(total)
    stmt = apply_catalog_filters(select(Product), **filters)
    result = await db.execute(
        stmt.options(
            selectinload(Product.variants).selectinload(ProductVariant.prices),
            selectinload(Product.variants).selectinload(ProductVariant.inventory),
        )
        .order_by(*await _product_order(db, order_by, order_dir))
        .offset(skip)
        .limit(limit)
    )
    products = result.scalars().all()
    rows = [_admin_product_row(p) for p in products]
    if pricing:
        await _with_rules(db, products, rows)
    return rows


async def _with_rules(db: AsyncSession, products, rows: list[AdminProductDetail]) -> None:
    """Each row's worksheet rule ("Fiyat Giriş Modu"); the products' variants loaded."""
    records = await bulk_erp.governing_records(db, list(products))
    for row in rows:
        if row.id in records:
            row.price_rule = bulk_erp.price_rule(records[row.id])


def _admin_product_row(product: Product) -> AdminProductDetail:
    return AdminProductDetail(
        id=product.id,
        base_code=product.base_code,
        name=product.name,
        brand=product.brand,
        category_id=product.category_id,
        tags=product.tags,
        image_url=product.image_url,
        is_published=product.is_published,
        erp_prices=product.erp_prices,
        variants=[
            AdminVariantDetail(
                id=v.id,
                sku=v.sku,
                color_name=v.color_name,
                attributes=v.attributes,
                cost_price=v.cost_price,
                weight_kg=v.weight_kg,
                prices=[AdminVariantPrice.model_validate(pr) for pr in v.prices],
                # The central stock only: the retired per-segment quota rows are not stock.
                inventory=[
                    AdminInventoryRead.model_validate(inv)
                    for inv in v.inventory
                    if WarehouseType(inv.warehouse_type) is CENTRAL_STOCK
                ],
            )
            # Live colours only, in the admin's order: the table's stock column is
            # the sum of these, and a retired colour has no stock to add.
            for v in sorted(
                (v for v in product.variants if v.is_active), key=lambda v: (v.position, v.sku)
            )
        ],
    )


async def _admin_product_rows(db: AsyncSession, ids: list[uuid.UUID]) -> list[AdminProductDetail]:
    """The given products as table rows, in the order asked for."""
    if not ids:
        return []
    rows = (
        await db.execute(
            select(Product)
            .where(Product.id.in_(ids))
            .options(
                selectinload(Product.variants).selectinload(ProductVariant.prices),
                selectinload(Product.variants).selectinload(ProductVariant.inventory),
            )
            .execution_options(populate_existing=True)
        )
    ).scalars()
    by_id = {p.id: p for p in rows}
    return [_admin_product_row(by_id[pid]) for pid in ids if pid in by_id]


@router.post("/products/bulk-erp-prices", response_model=BulkErpPricesResult)
async def bulk_erp_prices(
    payload: BulkErpPricesWrite,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """"Fiyat Giriş Modu"'s save: each typed cell is a product's TL ERP price for
    a group, written to its "ERP ve Katalog Fiyatları" and — through its
    worksheet's rule, as the worksheet would — to its site price
    (app.services.bulk_erp). One transaction: every cell, or none."""
    try:
        applied = await bulk_erp.apply_erp_changes(db, payload.changes)
    except bulk_erp.BulkErpError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    await db.commit()
    ids = list(dict.fromkeys(c.product_id for c in payload.changes))
    rows = await _admin_product_rows(db, ids)
    products = (
        await db.scalars(select(Product).where(Product.id.in_(ids)).options(selectinload(Product.variants)))
    ).all()
    await _with_rules(db, products, rows)
    return BulkErpPricesResult(applied=applied, products=rows)


@router.post("/products/delete", response_model=ProductDeleteResult)
async def delete_products(
    payload: ProductIds,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete the ticked products (one or many): a soft delete, so a product
    that appears in orders goes as well and its order lines keep it."""
    result = ProductDeleteResult()
    products = {
        p.id: p
        for p in (
            await db.scalars(select(Product).where(Product.id.in_(payload.product_ids), Product.is_deleted.is_(False)))
        ).all()
    }
    now = datetime.now(timezone.utc)
    for product_id in dict.fromkeys(payload.product_ids):
        product = products.get(product_id)
        if product is None:
            result.refused[product_id] = "Ürün bulunamadı"
            continue
        product_blueprint.soft_delete(product, now)
        result.deleted.append(product_id)
    await db.commit()
    return result


@router.post("/products/duplicate", response_model=list[AdminProductDetail], status_code=status.HTTP_201_CREATED)
async def duplicate_products(
    payload: ProductIds,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Deep-copy the ticked products as unpublished drafts, in the order given."""
    segments = {s.id: SegmentType(s.code) for s in (await db.execute(select(CustomerSegment))).scalars()}
    copies: list[uuid.UUID] = []
    for product_id in payload.product_ids:
        source = await product_blueprint.load(db, product_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Kopyalanacak ürün bulunamadı")
        try:
            copies.append((await product_blueprint.duplicate(db, source, segments)).id)
        except product_blueprint.SheetError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    await db.commit()
    return await _admin_product_rows(db, copies)


@router.patch("/products/{product_id}/publish", response_model=ProductPublishRead)
async def set_product_published(
    product_id: uuid.UUID,
    payload: ProductPublishUpdate,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Put a product on the storefront, or take it off, without opening its form."""
    product = await db.scalar(select(Product).where(Product.id == product_id, Product.is_deleted.is_(False)))
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    product.is_published = payload.is_published
    await db.commit()
    return ProductPublishRead(id=product.id, is_published=product.is_published)


@router.patch("/products/{product_id}/stock", response_model=AdminProductDetail)
async def set_product_stock(
    product_id: uuid.UUID,
    payload: ProductStockUpdate,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """The stock column's quick edit: set each given colour's stock without
    opening the product's form (and without its prices, flash sale or
    translations being saved again). Returns the table row as it now is."""
    product = await db.scalar(
        select(Product)
        .where(Product.id == product_id, Product.is_deleted.is_(False))
        .options(selectinload(Product.variants))
    )
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    variants = {v.id: v for v in product.variants if v.is_active}
    unknown = [str(item.id) for item in payload.variants if item.id not in variants]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Bu ürünün rengi değil: {', '.join(unknown)}")
    for item in payload.variants:
        await product_blueprint.sync_stock(db, variants[item.id], SheetStock(quantity=item.quantity))
    await db.commit()
    return (await _admin_product_rows(db, [product.id]))[0]


@router.get("/segments", response_model=list[SegmentRead])
async def list_segments(
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(CustomerSegment).order_by(CustomerSegment.id))
    return result.scalars().all()
