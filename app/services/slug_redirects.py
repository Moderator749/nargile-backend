"""Old slugs of products and categories, kept answering after an admin renames them.

Written by the admin's saves (the product sheet, the category form); read by the
public product and category lookups, which hand back the row under its current
slug so the storefront can send the visitor there for good.
"""

import uuid
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import SlugRedirect

Kind = Literal["product", "category"]


async def moved(
    db: AsyncSession,
    kind: Kind,
    old_slug: str,
    new_slug: str,
    *,
    product_id: uuid.UUID | None = None,
    category_id: int | None = None,
) -> None:
    """Record a rename: `old_slug` now leads to this row, and `new_slug`, being
    live again, is nobody's redirect any more."""
    await release(db, kind, new_slug)
    if old_slug == new_slug:
        return
    # The old address may have pointed somewhere else before; it points here now.
    await db.execute(delete(SlugRedirect).where(SlugRedirect.kind == kind, SlugRedirect.old_slug == old_slug))
    db.add(SlugRedirect(kind=kind, old_slug=old_slug, product_id=product_id, category_id=category_id))


async def release(db: AsyncSession, kind: Kind, slug: str) -> None:
    """A slug in live use: whatever redirect it was gives way to it."""
    await db.execute(delete(SlugRedirect).where(SlugRedirect.kind == kind, SlugRedirect.old_slug == slug))


async def product_id_for(db: AsyncSession, old_slug: str) -> uuid.UUID | None:
    return await db.scalar(
        select(SlugRedirect.product_id).where(SlugRedirect.kind == "product", SlugRedirect.old_slug == old_slug)
    )


async def category_id_for(db: AsyncSession, old_slug: str) -> int | None:
    return await db.scalar(
        select(SlugRedirect.category_id).where(SlugRedirect.kind == "category", SlugRedirect.old_slug == old_slug)
    )
