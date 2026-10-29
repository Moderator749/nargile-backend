"""The storefront's root namespace, /<slug>, shared by products, categories and brands.

Since 2026-10-29 all three kinds of page are at the root, so a slug names one
thing only: none may take another kind's slug (a product's or category's old
one, which still leads to it, included), nor one of the storefront's own
pages or redirected old addresses (RESERVED_ROOT_PATHS). Clashes within a
kind are each writer's own check (the product sheet, the category and brand
forms). Writers check a slug when it is new or changed, so a row that already
shares one (a brand's category, until it is retired) can still be saved.
"""

from typing import Literal

from sqlalchemy import or_, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.slugs import RESERVED_ROOT_PATHS
from app.models.brand import Brand
from app.models.catalog import Category, Product, SlugRedirect

Kind = Literal["product", "category", "brand"]

_PAGE = {"product": "bir ürünün", "category": "bir kategorinin", "brand": "bir markanın"}


def _holders(kind: Kind, slug: str):
    """(what, query) for every other kind's claim on `slug`."""
    claims = {
        "product": [
            select(Product.id).where(Product.slug == slug),
            select(SlugRedirect.id).where(SlugRedirect.kind == "product", SlugRedirect.old_slug == slug),
        ],
        "category": [
            select(Category.id).where(Category.slug == slug),
            select(SlugRedirect.id).where(SlugRedirect.kind == "category", SlugRedirect.old_slug == slug),
        ],
        "brand": [select(Brand.id).where(Brand.slug == slug)],
    }
    return [(other, query) for other, queries in claims.items() if other != kind for query in queries]


async def taken_by(db: AsyncSession, kind: Kind, slug: str) -> str | None:
    """Why `slug` cannot be this kind's: the Turkish reason for the admin, or None."""
    if slug in RESERVED_ROOT_PATHS:
        return f"“{slug}” adresi sitenin kendi sayfalarından birine ait; başka bir adres yazın"
    for other, query in _holders(kind, slug):
        if await db.scalar(query.limit(1)) is not None:
            return (
                f"“{slug}” adresi {_PAGE[other]} sayfası "
                "(ürünler, kategoriler ve markalar sitenin kökünde aynı adres alanını paylaşır)"
            )
    return None


def others_like(base: str):
    """Category and brand slugs, live or old, that a new product's slug `base` (or base-2…) would meet."""
    like = lambda column: or_(column == base, column.like(f"{base}-%"))  # noqa: E731
    return union(
        select(Category.slug).where(like(Category.slug)),
        select(SlugRedirect.old_slug).where(SlugRedirect.kind == "category", like(SlugRedirect.old_slug)),
        select(Brand.slug).where(like(Brand.slug)),
    )
