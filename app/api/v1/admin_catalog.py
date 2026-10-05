"""Admin-only catalog and inventory management endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.models.catalog import (
    Inventory,
    Product,
    ProductVariant,
    VariantPrice,
    WarehouseType,
)
from app.models.user import User
from app.schemas.admin import (
    AdminProductCreate,
    AdminVariantCreate,
    AdminVariantRead,
    PricingMatrixRead,
    PricingMatrixUpdate,
    SegmentPriceRow,
)
from app.schemas.catalog import ProductRead

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
