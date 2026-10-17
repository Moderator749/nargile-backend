from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.catalog import Category, Product
from app.schemas.catalog import CategoryRead
from app.services import slug_redirects
from app.services.categories import memberships

# Public, read-only. Category writes belong in the admin API.
router = APIRouter(prefix="/categories", tags=["categories"])


async def _visible(db: AsyncSession) -> list[Category]:
    """Active categories whose parent (if any) is active too: what the storefront shows."""
    result = await db.execute(select(Category).order_by(Category.sort_order, Category.name, Category.id))
    rows = result.scalars().all()
    active = {c.id for c in rows if c.is_active}
    return [c for c in rows if c.is_active and (c.parent_id is None or c.parent_id in active)]


@router.get("/by-slug/{slug}", response_model=CategoryRead)
async def category_by_slug(slug: str = Path(max_length=180), db: AsyncSession = Depends(get_db)):
    """One visible category by its slug — or by a slug the admin has since
    changed, answered under its current one, so the category page can send the
    visitor on for good. 404 when neither names a visible category."""
    slug = slug.lower()
    visible = await _visible(db)
    found = next((c for c in visible if c.slug == slug), None)
    if found is None:
        renamed = await slug_redirects.category_id_for(db, slug)
        found = next((c for c in visible if c.id == renamed), None) if renamed else None
    if found is None:
        raise HTTPException(status_code=404, detail="Category not found")
    return found


@router.get("", response_model=list[CategoryRead])
async def list_categories(db: AsyncSession = Depends(get_db)):
    """The active categories. A subcategory of an inactive parent is hidden with
    it, so switching a parent off takes its whole branch out of the menus.

    Each says how many published products its page lists (its subcategories'
    included, a product in two of them counted once) and when the newest of
    them was last edited — what the sitemap needs, in one request."""
    rows = (await db.execute(select(Category))).scalars().all()
    visible = await _visible(db)

    listed = memberships().subquery()
    pairs = (
        await db.execute(
            select(listed.c.category_id, Product.id, Product.updated_at)
            .join(Product, Product.id == listed.c.product_id)
            .where(Product.is_published.is_(True))
        )
    ).all()
    edited: dict = {}
    direct: dict[int, set] = {}
    for category_id, product_id, updated_at in pairs:
        direct.setdefault(category_id, set()).add(product_id)
        edited[product_id] = updated_at
    children: dict[int, list[int]] = {}
    for c in rows:
        if c.parent_id is not None:
            children.setdefault(c.parent_id, []).append(c.id)

    def subtree_products(category_id: int, seen: frozenset = frozenset()) -> set:
        if category_id in seen:  # a cycle would be a data error; never loop on it
            return set()
        found = set(direct.get(category_id, ()))
        for child in children.get(category_id, ()):
            found |= subtree_products(child, seen | {category_id})
        return found

    out = []
    for c in visible:
        products = subtree_products(c.id)
        view = CategoryRead.model_validate(c)
        view.product_count = len(products)
        view.updated_at = max((edited[p] for p in products), default=None)
        out.append(view)
    return out
