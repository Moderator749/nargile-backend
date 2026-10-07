"""Admin-only flash sale management ("Fırsat Ürünleri")."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.models.catalog import CurrencyType, ProductVariant
from app.models.promotion import FlashSale
from app.models.user import CustomerSegment, SegmentType, User
from app.schemas.promotion import FlashSaleRead, FlashSaleWrite

router = APIRouter(prefix="/admin/flash-sales", tags=["admin-promotions"])

_LOADS = (
    selectinload(FlashSale.variant).selectinload(ProductVariant.product),
    selectinload(FlashSale.variant).selectinload(ProductVariant.prices),
)


async def _retail_id(db: AsyncSession) -> int:
    return await db.scalar(select(CustomerSegment.id).where(CustomerSegment.code == SegmentType.RETAIL))


def _read(sale: FlashSale, retail_id: int, now: datetime) -> FlashSaleRead:
    regular = next(
        (
            p.price_incl_tax
            for p in sale.variant.prices
            if p.segment_id == retail_id and p.currency == CurrencyType.TRY
        ),
        None,
    )
    return FlashSaleRead(
        id=sale.id,
        variant_id=sale.variant_id,
        sku=sale.variant.sku,
        product_name=sale.variant.product.name,
        sale_price_incl_tax=sale.sale_price_incl_tax,
        regular_price_incl_tax=regular,
        starts_at=sale.starts_at,
        ends_at=sale.ends_at,
        is_active=sale.is_active,
        is_live=sale.is_live(now),
    )


async def _load(db: AsyncSession, sale_id: int) -> FlashSale:
    sale = await db.scalar(
        select(FlashSale)
        .where(FlashSale.id == sale_id)
        .options(*_LOADS)
        .execution_options(populate_existing=True)  # fresh relationships after an edit
    )
    if sale is None:
        raise HTTPException(status_code=404, detail="Flash sale not found")
    return sale


async def _apply(db: AsyncSession, sale: FlashSale, payload: FlashSaleWrite) -> None:
    """Resolve the SKU and check the sale is really a discount on the RETAIL price."""
    variant = await db.scalar(
        select(ProductVariant).where(ProductVariant.sku == payload.sku).options(selectinload(ProductVariant.prices))
    )
    if variant is None:
        raise HTTPException(status_code=404, detail=f"No variant with SKU {payload.sku}")

    clash = await db.scalar(
        select(FlashSale.id).where(FlashSale.variant_id == variant.id, FlashSale.id != sale.id)
    )
    if clash is not None:
        raise HTTPException(status_code=409, detail="This variant already has a flash sale; edit that one")

    retail_id = await _retail_id(db)
    regular = next(
        (p.price_incl_tax for p in variant.prices if p.segment_id == retail_id and p.currency == CurrencyType.TRY),
        None,
    )
    if regular is None:
        raise HTTPException(status_code=422, detail="Variant has no RETAIL price to discount")
    if payload.sale_price_incl_tax >= regular:
        raise HTTPException(status_code=422, detail=f"Sale price must be below the RETAIL price ({regular})")

    sale.variant_id = variant.id
    sale.sale_price_incl_tax = payload.sale_price_incl_tax
    sale.starts_at = payload.starts_at
    sale.ends_at = payload.ends_at
    sale.is_active = payload.is_active


@router.get("", response_model=list[FlashSaleRead])
async def list_flash_sales(
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(FlashSale).options(*_LOADS).order_by(FlashSale.ends_at.desc(), FlashSale.id))
    retail_id, now = await _retail_id(db), datetime.now(timezone.utc)
    return [_read(s, retail_id, now) for s in result.scalars()]


@router.post("", response_model=FlashSaleRead, status_code=status.HTTP_201_CREATED)
async def create_flash_sale(
    payload: FlashSaleWrite,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    sale = FlashSale()
    await _apply(db, sale, payload)
    db.add(sale)
    await db.commit()
    sale = await _load(db, sale.id)
    return _read(sale, await _retail_id(db), datetime.now(timezone.utc))


@router.put("/{sale_id}", response_model=FlashSaleRead)
async def update_flash_sale(
    sale_id: int,
    payload: FlashSaleWrite,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    sale = await _load(db, sale_id)
    await _apply(db, sale, payload)
    await db.commit()
    sale = await _load(db, sale_id)
    return _read(sale, await _retail_id(db), datetime.now(timezone.utc))


@router.delete("/{sale_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_flash_sale(
    sale_id: int,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    await db.delete(await _load(db, sale_id))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
