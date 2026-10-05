from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.models.catalog import Product, ProductVariant
from app.schemas.catalog import (
    ProductCreate,
    ProductRead,
    VariantCreate,
    VariantRead,
)

router = APIRouter(prefix="/products", tags=["products"])


@router.get("/", response_model=list[ProductRead])
async def list_products(
    skip: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Product).offset(skip).limit(limit))
    return result.scalars().all()


@router.post("/", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
async def create_product(payload: ProductCreate, db: AsyncSession = Depends(get_db)):
    product = Product(**payload.model_dump())
    db.add(product)
    await db.commit()
    await db.refresh(product)
    return product


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


@router.post(
    "/{product_id}/variants",
    response_model=VariantRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_variant(
    product_id: str,
    payload: VariantCreate,
    db: AsyncSession = Depends(get_db),
):
    variant = ProductVariant(product_id=product_id, **payload.model_dump())
    db.add(variant)
    await db.commit()
    await db.refresh(variant)
    return variant
