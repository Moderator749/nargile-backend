"""Admin-only catalog and inventory management endpoints."""

import uuid

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
    ProductDeleteResult,
    ProductIds,
    ProductSort,
    SortDirection,
    AdminProductDetail,
    AdminVariantDetail,
    ProductPublishRead,
    ProductPublishUpdate,
    AdminVariantPrice,
    SegmentRead,
)
from app.schemas.product_sheet import ProductSheetRead, ProductSheetWrite
from app.services import product_blueprint
from app.services.checkout_engine import CENTRAL_STOCK
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
    retail_price = (
        select(func.min(VariantPrice.price_incl_tax))
        .select_from(VariantPrice)
        .join(ProductVariant, ProductVariant.id == VariantPrice.variant_id)
        .where(
            ProductVariant.product_id == Product.id,
            ProductVariant.is_active.is_(True),
            VariantPrice.segment_id == retail_id,
            VariantPrice.currency == CurrencyType.TRY,
        )
        .correlate(Product)
        .scalar_subquery()
    )

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
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Products with every segment price and their stock, for the admin panel.
    The X-Total-Count header carries the number of matches, for page controls."""
    total = await db.scalar(
        select(func.count()).select_from(apply_catalog_filters(select(Product.id), category=category, q=q).subquery())
    )
    response.headers["X-Total-Count"] = str(total)
    stmt = apply_catalog_filters(select(Product), category=category, q=q)
    result = await db.execute(
        stmt.options(
            selectinload(Product.variants).selectinload(ProductVariant.prices),
            selectinload(Product.variants).selectinload(ProductVariant.inventory),
        )
        .order_by(*await _product_order(db, order_by, order_dir))
        .offset(skip)
        .limit(limit)
    )
    return [_admin_product_row(p) for p in result.scalars().all()]


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


@router.post("/products/delete", response_model=ProductDeleteResult)
async def delete_products(
    payload: ProductIds,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete the ticked products, with everything that exists only for them.

    A product that appears in an order is kept and reported back: deleting it would
    take order lines with it. Unpublishing is the way to retire such a product.
    """
    result = ProductDeleteResult()
    for product_id in payload.product_ids:
        product = await db.scalar(
            select(Product).where(Product.id == product_id).options(selectinload(Product.variants))
        )
        if product is None:
            result.refused[product_id] = "Ürün bulunamadı"
            continue
        if await product_blueprint.is_sold(db, product):
            result.refused[product_id] = f"“{product.name}” siparişlerde kullanılıyor, yayından alabilirsiniz"
            continue
        await product_blueprint.delete_product(db, product)
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
    product = await db.scalar(select(Product).where(Product.id == product_id))
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    product.is_published = payload.is_published
    await db.commit()
    return ProductPublishRead(id=product.id, is_published=product.is_published)


@router.get("/segments", response_model=list[SegmentRead])
async def list_segments(
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(CustomerSegment).order_by(CustomerSegment.id))
    return result.scalars().all()
