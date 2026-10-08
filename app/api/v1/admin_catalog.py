"""Admin-only catalog and inventory management endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.models.catalog import (
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
    AdminProductCreate,
    AdminProductDetail,
    AdminVariantDetail,
    ProductPublishRead,
    ProductPublishUpdate,
    AdminVariantPrice,
    SegmentRead,
    AdminVariantCreate,
    AdminVariantRead,
    PricingMatrixRead,
    PricingMatrixUpdate,
    SegmentPriceRow,
    VariantWeightRead,
    VariantWeightUpdate,
)
from app.schemas.catalog import ProductRead
from app.schemas.product_sheet import ProductSheetRead, ProductSheetWrite
from app.services import product_blueprint

router = APIRouter(prefix="/admin", tags=["admin-catalog"])


# ── Products ─────────────────────────────────────────────────────────────────

@router.post(
    "/products",
    response_model=ProductRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_product(
    payload: AdminProductCreate,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    product = Product(**payload.model_dump())
    db.add(product)
    await db.commit()
    await db.refresh(product)
    return product


# ── The product sheet ("Ürün Ekle / Düzenle") ────────────────────────────────

@router.post("/products/sheet", response_model=ProductSheetRead, status_code=status.HTTP_201_CREATED)
async def create_product_sheet(
    payload: ProductSheetWrite,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a product from the whole sheet: info, stock quotas, price matrix,
    rule flags and the Fırsat block, in one transaction."""
    try:
        product = await product_blueprint.save(db, payload)
    except product_blueprint.SheetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await db.commit()
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
            Inventory.warehouse_type == WarehouseType.RETAIL_STOCK,
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
    """Products with every segment price and all stock quotas, for the admin panel.
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
                inventory=[AdminInventoryRead.model_validate(inv) for inv in v.inventory],
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


# ── Variants + initial inventory ─────────────────────────────────────────────

@router.post(
    "/products/{product_id}/variants",
    response_model=AdminVariantRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_variant(
    product_id: str,
    payload: AdminVariantCreate,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    # Verify product exists
    result = await db.execute(select(Product).where(Product.id == product_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Product not found")

    # Check SKU uniqueness
    sku_check = await db.execute(
        select(ProductVariant).where(ProductVariant.sku == payload.sku)
    )
    if sku_check.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="SKU already exists")

    variant = ProductVariant(
        product_id=product_id,
        sku=payload.sku,
        barcode=payload.barcode,
        attributes=payload.attributes,
        cost_price=payload.cost_price,
    )
    db.add(variant)
    await db.flush()

    # Create initial inventory rows
    inventory_rows = []
    if payload.initial_retail_stock > 0:
        inv = Inventory(
            variant_id=variant.id,
            warehouse_type=WarehouseType.RETAIL_STOCK,
            quantity=payload.initial_retail_stock,
        )
        db.add(inv)
        inventory_rows.append(inv)

    if payload.initial_b2b_stock > 0:
        inv = Inventory(
            variant_id=variant.id,
            warehouse_type=WarehouseType.B2B_STOCK,
            quantity=payload.initial_b2b_stock,
        )
        db.add(inv)
        inventory_rows.append(inv)

    await db.commit()

    # Reload with inventory for response
    result = await db.execute(
        select(ProductVariant)
        .where(ProductVariant.id == variant.id)
        .options(selectinload(ProductVariant.inventory))
    )
    return result.scalar_one()


# ── Pricing matrix ───────────────────────────────────────────────────────────

@router.put(
    "/variants/{variant_id}/pricing",
    response_model=PricingMatrixRead,
)
async def update_pricing_matrix(
    variant_id: str,
    payload: PricingMatrixUpdate,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    # Verify variant exists
    result = await db.execute(
        select(ProductVariant).where(ProductVariant.id == variant_id)
    )
    variant = result.scalar_one_or_none()
    if not variant:
        raise HTTPException(status_code=404, detail="Variant not found")

    upserted: list[VariantPrice] = []

    for sp in payload.prices:
        # Try to find existing price row for this segment+currency
        existing = await db.execute(
            select(VariantPrice).where(
                VariantPrice.variant_id == variant.id,
                VariantPrice.segment_id == sp.segment_id,
                VariantPrice.currency == payload.currency,
            )
        )
        price_row = existing.scalar_one_or_none()

        if price_row:
            price_row.price_excl_tax = sp.price_excl_tax
            price_row.price_incl_tax = sp.price_incl_tax
            price_row.vat_rate = sp.vat_rate
        else:
            price_row = VariantPrice(
                variant_id=variant.id,
                segment_id=sp.segment_id,
                currency=payload.currency,
                price_excl_tax=sp.price_excl_tax,
                price_incl_tax=sp.price_incl_tax,
                vat_rate=sp.vat_rate,
            )
            db.add(price_row)

        await db.flush()
        upserted.append(price_row)

    await db.commit()

    return PricingMatrixRead(
        variant_id=variant.id,
        currency=payload.currency,
        prices=[
            SegmentPriceRow(
                id=p.id,
                segment_id=p.segment_id,
                price_excl_tax=p.price_excl_tax,
                price_incl_tax=p.price_incl_tax,
                vat_rate=p.vat_rate,
            )
            for p in upserted
        ],
    )


@router.patch("/variants/{variant_id}", response_model=VariantWeightRead)
async def update_variant_weight(
    variant_id: uuid.UUID,
    payload: VariantWeightUpdate,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Set a variant's unit weight (used by the coal free-shipping rule)."""
    variant = await db.get(ProductVariant, variant_id)
    if variant is None:
        raise HTTPException(status_code=404, detail="Variant not found")
    variant.weight_kg = payload.weight_kg
    await db.commit()
    return VariantWeightRead(id=variant.id, sku=variant.sku, weight_kg=variant.weight_kg)
