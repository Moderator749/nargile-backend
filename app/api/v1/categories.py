from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_optional_user
from app.core.database import get_db
from app.models.catalog import Category, Product
from app.schemas.catalog import CategoryRead
from app.schemas.i18n import Locale
from app.services import localize, slug_redirects
from app.models.user import User
from app.services.categories import memberships, visible_ids
from app.services.tier_access import Restrictions, refuse, restrictions_for, visible as visible_to

# Public, read-only. Category writes belong in the admin API.
router = APIRouter(prefix="/categories", tags=["categories"])


async def _visible(db: AsyncSession, access: Restrictions | None = None) -> list[Category]:
    """Active categories with every category above them active too: what the
    storefront shows — less, with `access`, what the viewer's group may not see."""
    result = await db.execute(select(Category).order_by(Category.sort_order, Category.name, Category.id))
    rows = result.scalars().all()
    shown = visible_ids((c.id, c.parent_id, c.is_active) for c in rows)
    hidden = access.category_ids if access is not None else frozenset()
    return [c for c in rows if c.id in shown and c.id not in hidden]


@router.get("/by-slug/{slug}", response_model=CategoryRead)
async def category_by_slug(
    slug: str = Path(max_length=180),
    lang: Locale | None = Query(None, description="en | ar — its name in that language"),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
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
    if found.id in (await restrictions_for(db, user)).category_ids:
        refuse()  # "Grup Erişim Kısıtlamaları": a notice, not the category
    return localize.category_view(CategoryRead.model_validate(found), found.translations, lang)


@router.get("", response_model=list[CategoryRead])
async def list_categories(
    lang: Locale | None = Query(None, description="en | ar — the names in that language"),
    user: User | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
):
    """The active categories — less those the viewer's group may not see. A subcategory of an inactive one, at any depth, is
    hidden with it, so switching a category off takes its whole branch out.

    Each says how many published products its page lists (its subcategories'
    included, a product in two of them counted once) and when the newest of
    them was last edited — what the sitemap needs, in one request."""
    rows = (await db.execute(select(Category))).scalars().all()
    access = await restrictions_for(db, user)
    visible = await _visible(db, access)

    listed = memberships().subquery()
    pairs = (
        await db.execute(
            select(listed.c.category_id, Product.id, Product.updated_at)
            .join(Product, Product.id == listed.c.product_id)
            .where(Product.on_storefront(), visible_to(access))
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
        out.append(localize.category_view(view, c.translations, lang))
    return out
