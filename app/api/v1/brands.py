"""Public brand list (menu "Markalar" and the homepage marquee)."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_optional_user
from app.core.database import get_db
from app.models.brand import Brand
from app.schemas.taxonomy import BrandRead
from app.models.user import User
from app.services.media import media_url
from app.services.tier_access import restrictions_for

router = APIRouter(prefix="/brands", tags=["brands"])


def brand_read(brand: Brand, request: Request) -> BrandRead:
    return BrandRead(
        id=brand.id,
        name=brand.name,
        slug=brand.slug,
        search_term=brand.search_term,
        logo_url=media_url(request, brand.logo_path) if brand.logo_path else None,
        description=brand.description,
        is_active=brand.is_active,
        sort_order=brand.sort_order,
    )


@router.get("", response_model=list[BrandRead])
async def list_brands(
    request: Request, user: User | None = Depends(get_optional_user), db: AsyncSession = Depends(get_db)
):
    """The active brands — less those the viewer's group may not see."""
    hidden = (await restrictions_for(db, user)).brand_ids
    result = await db.execute(
        select(Brand)
        .where(Brand.is_active.is_(True), *([Brand.id.notin_(hidden)] if hidden else []))
        .order_by(Brand.sort_order, Brand.name)
    )
    return [brand_read(b, request) for b in result.scalars()]
