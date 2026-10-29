"""The storefront's root namespace, /<slug>, shared by products and categories.

Since 2026-10-29 both kinds of page are at the root, so a slug names one thing
only: a product may not take a category's slug (live, or an old one that
still leads to it), nor a category a product's, and neither one of the
storefront's own pages or redirected old addresses (RESERVED_ROOT_PATHS).
Clashes within a kind are each writer's own check (the product sheet, the
category form).
"""

from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.slugs import RESERVED_ROOT_PATHS
from app.models.catalog import Category, Product, SlugRedirect

Kind = Literal["product", "category"]


async def taken_by(db: AsyncSession, kind: Kind, slug: str) -> str | None:
    """Why `slug` cannot be this kind's: the Turkish reason for the admin, or None."""
    if slug in RESERVED_ROOT_PATHS:
        return f"“{slug}” adresi sitenin kendi sayfalarından birine ait; başka bir adres yazın"
    if kind == "product":
        live = select(Category.id).where(Category.slug == slug)
        old = select(SlugRedirect.id).where(SlugRedirect.kind == "category", SlugRedirect.old_slug == slug)
        other = "bir kategorinin"
    else:
        live = select(Product.id).where(Product.slug == slug)
        old = select(SlugRedirect.id).where(SlugRedirect.kind == "product", SlugRedirect.old_slug == slug)
        other = "bir ürünün"
    if await db.scalar(live.limit(1)) is not None or await db.scalar(old.limit(1)) is not None:
        return f"“{slug}” adresi {other} sayfası (ürünler ve kategoriler sitenin kökünde aynı adres alanını paylaşır)"
    return None


def category_slugs_like(base: str):
    """Category slugs, live or old, that a new product's slug `base` (or base-2…) would meet."""
    return select(Category.slug).where(or_(Category.slug == base, Category.slug.like(f"{base}-%"))).union(
        select(SlugRedirect.old_slug).where(
            SlugRedirect.kind == "category",
            or_(SlugRedirect.old_slug == base, SlugRedirect.old_slug.like(f"{base}-%")),
        )
    )

