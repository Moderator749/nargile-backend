"""IndexNow: tell Bing (and every engine sharing api.indexnow.org: Yandex,
Seznam, Naver; ChatGPT search reads Bing's index) which storefront pages an
admin save just changed, so they are re-crawled within minutes instead of
whenever the crawler next passes.

The admin endpoints call `queue_products` / `queue_categories` / `queue_brands`
as background tasks after their commit. The URLs gather for DEBOUNCE_SECONDS
and go out in one POST, so a bulk save (a price import, a category's bulk
settings) is one request, not hundreds; the wait also lets the storefront's
own revalidation (the panel's /api/revalidate call after every save) land
before the engines fetch.

Off unless INDEXNOW_KEY is set, and with BACKGROUND_JOBS=false (a copy of the
shop must not announce pages). The key's file is the storefront's
public/<key>.txt. A failed ping is logged and dropped: IndexNow is a hint, and
the sitemap still lists every page.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterable
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models.brand import Brand
from app.models.catalog import Category, Product
from app.services.categories import memberships
from app.services.localize import locales_of

logger = logging.getLogger(__name__)

#: How long URLs gather before they are sent together.
DEBOUNCE_SECONDS = 30.0
#: The protocol's limit per request.
BATCH = 10_000

_pending: set[str] = set()
_flush_task: asyncio.Task | None = None


def enabled() -> bool:
    return bool(settings.INDEXNOW_KEY) and settings.BACKGROUND_JOBS


def page_urls(slug: str | None, locales: Iterable[str] = ("tr",)) -> list[str]:
    """A root-level storefront page (/<slug>: product, category or brand) in
    each language it is really in: Turkish at the root, the others under
    /<lang> (the storefront's localePath)."""
    if not slug:
        return []
    base = settings.INDEXNOW_SITE_URL.rstrip("/")
    return [f"{base}/{slug}" if lang == "tr" else f"{base}/{lang}/{slug}" for lang in dict.fromkeys(["tr", *locales])]


def queue(urls: Iterable[str]) -> None:
    """Add URLs to the next ping and make sure one is scheduled."""
    global _flush_task
    if not enabled():
        return
    _pending.update(u for u in urls if u)
    if _pending and (_flush_task is None or _flush_task.done()):
        _flush_task = asyncio.get_running_loop().create_task(_flush_later())


async def _flush_later() -> None:
    await asyncio.sleep(DEBOUNCE_SECONDS)
    await flush()


async def flush() -> None:
    """Send everything queued so far."""
    urls = sorted(_pending)
    _pending.clear()
    for start in range(0, len(urls), BATCH):
        await send(urls[start : start + BATCH])


async def send(urls: list[str]) -> None:
    """One IndexNow POST (replaced in the tests)."""
    if not urls:
        return
    base = settings.INDEXNOW_SITE_URL.rstrip("/")
    body = {
        "host": urlsplit(base).hostname,
        "key": settings.INDEXNOW_KEY,
        "keyLocation": f"{base}/{settings.INDEXNOW_KEY}.txt",
        "urlList": urls,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(settings.INDEXNOW_ENDPOINT, json=body)
        if response.status_code in (200, 202):
            logger.info("IndexNow: %d URL(s) accepted (%d)", len(urls), response.status_code)
        else:
            logger.warning("IndexNow: %d URL(s) refused (%d): %s", len(urls), response.status_code, response.text[:200])
    except httpx.HTTPError:
        logger.warning("IndexNow: ping failed", exc_info=True)


# ── What a save changed ──────────────────────────────────────────────────────


async def _category_urls(db: AsyncSession, ids: Iterable[int]) -> list[str]:
    ids = list(dict.fromkeys(i for i in ids if i is not None))
    if not ids:
        return []
    rows = (await db.scalars(select(Category).where(Category.id.in_(ids)))).all()
    return [u for c in rows for u in page_urls(c.slug, locales_of(c.translations))]


async def _brand_urls(db: AsyncSession, ids: Iterable[int]) -> list[str]:
    ids = list(dict.fromkeys(i for i in ids if i is not None))
    if not ids:
        return []
    rows = (await db.scalars(select(Brand).where(Brand.id.in_(ids), Brand.is_active.is_(True)))).all()
    return [u for b in rows for u in page_urls(b.slug)]


async def product_urls(db: AsyncSession, product_ids: Iterable[uuid.UUID]) -> list[str]:
    """Each product's page (in its live languages) and the category and brand
    pages that list it, whose grids show its price and stock. An unpublished
    or deleted product is announced too: its address now redirects to its
    shelf, which the engines should learn."""
    ids = list(dict.fromkeys(product_ids))
    if not ids:
        return []
    products = (await db.scalars(select(Product).where(Product.id.in_(ids)))).all()
    listed = memberships().subquery()
    category_ids = (await db.scalars(select(listed.c.category_id).where(listed.c.product_id.in_(ids)))).all()
    urls = [u for p in products for u in page_urls(p.slug, locales_of(p.translations))]
    urls += await _category_urls(db, category_ids)
    urls += await _brand_urls(db, (p.brand_id for p in products))
    return urls


# ── Background tasks for the admin endpoints (each opens its own session) ─────


async def queue_products(sessions: async_sessionmaker, product_ids: Iterable[uuid.UUID]) -> None:
    if not enabled():
        return
    try:
        async with sessions() as db:
            queue(await product_urls(db, list(product_ids)))
    except Exception:  # noqa: BLE001 — a hint must never break the save that sent it
        logger.warning("IndexNow: could not collect product URLs", exc_info=True)


async def queue_categories(sessions: async_sessionmaker, category_ids: Iterable[int]) -> None:
    if not enabled():
        return
    try:
        async with sessions() as db:
            queue(await _category_urls(db, list(category_ids)))
    except Exception:  # noqa: BLE001
        logger.warning("IndexNow: could not collect category URLs", exc_info=True)


async def queue_brands(sessions: async_sessionmaker, brand_ids: Iterable[int]) -> None:
    if not enabled():
        return
    try:
        async with sessions() as db:
            queue(await _brand_urls(db, list(brand_ids)))
    except Exception:  # noqa: BLE001
        logger.warning("IndexNow: could not collect brand URLs", exc_info=True)
