"""Public brand list (menu "Markalar" and the homepage marquee)."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.brand import Brand
from app.schemas.taxonomy import BrandRead
from app.services.media import media_url

router = APIRouter(prefix="/brands", tags=["brands"])


def brand_read(brand: Brand, request: Request) -> BrandRead:
    return BrandRead(
        id=brand.id,
        name=brand.name,
        slug=brand.slug,
        search_term=brand.search_term,
        logo_url=media_url(request, brand.logo_path) if brand.logo_path else None,
        is_active=brand.is_active,
        sort_order=brand.sort_order,
    )


@router.get("", response_model=list[BrandRead])
async def list_brands(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Brand).where(Brand.is_active.is_(True)).order_by(Brand.sort_order, Brand.name)
    )
    return [brand_read(b, request) for b in result.scalars()]
