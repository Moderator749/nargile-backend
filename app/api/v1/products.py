from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_optional_user
from app.core.database import get_db
from app.models.catalog import CurrencyType, Product, ProductVariant
from app.models.user import CustomerSegment, SegmentType, User
from app.schemas.catalog import (
    CatalogPrice,
    CatalogProductRead,
    CatalogVariantRead,
    ProductRead,
)

# Read-only. Catalog writes live in admin_catalog.py behind admin auth.
router = APIRouter(prefix="/products", tags=["products"])


def _price_for(variant: ProductVariant, segment_id: int) -> CatalogPrice | None:
    for p in variant.prices:
        if p.segment_id == segment_id and p.currency == CurrencyType.TRY:
            return CatalogPrice.model_validate(p, from_attributes=True)
    return None


@router.get("/", response_model=list[CatalogProductRead])
async def list_products(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """Catalog page. Prices are resolved for the caller's segment (RETAIL when
    anonymous), matching what checkout will charge."""
    retail_id = await db.scalar(
        select(CustomerSegment.id).where(CustomerSegment.code == SegmentType.RETAIL)
    )
    segment_id = (user.segment_id if user else None) or retail_id

    result = await db.execute(
        select(Product)
        .options(selectinload(Product.variants).selectinload(ProductVariant.prices))
        .order_by(Product.name, Product.id)
        .offset(skip)
        .limit(limit)
    )
    products = result.scalars().all()

    return [
        CatalogProductRead(
            **ProductRead.model_validate(product).model_dump(),
            variants=[
                CatalogVariantRead(
                    id=v.id,
                    sku=v.sku,
                    attributes=v.attributes,
                    price=_price_for(v, segment_id),
                    list_price=_price_for(v, retail_id),
                )
                for v in sorted(product.variants, key=lambda v: v.sku)
            ],
        )
        for product in products
    ]


@router.get("/{product_id}", response_model=ProductRead)
async def get_product(product_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Product)
        .where(Product.id == product_id)
        .options(selectinload(Product.variants))
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product
