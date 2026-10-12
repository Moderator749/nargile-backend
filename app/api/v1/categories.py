from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.catalog import Category
from app.schemas.catalog import CategoryRead

# Public, read-only. Category writes belong in the admin API.
router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("/", response_model=list[CategoryRead])
async def list_categories(db: AsyncSession = Depends(get_db)):
    """The active categories. A subcategory of an inactive parent is hidden with
    it, so switching a parent off takes its whole branch out of the menus."""
    result = await db.execute(select(Category).order_by(Category.sort_order, Category.name, Category.id))
    rows = result.scalars().all()
    active = {c.id for c in rows if c.is_active}
    return [c for c in rows if c.is_active and (c.parent_id is None or c.parent_id in active)]
