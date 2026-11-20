"""Asking the storefront to drop its cached catalog after a change made here,
in the backend, that no admin's panel will announce: Claude's translation
drafts, and later the price-intelligence pushes.

The storefront's POST /api/revalidate (with REVALIDATE_SECRET) drops the whole
"catalog" cache — every product, shelf and brand page and the Merchant feed —
so one call covers any number of changes. Requests are therefore gathered the
way the panel gathers its own (saas_frontend/src/lib/revalidate.ts): the call
goes out QUIET_SECONDS after the last request, and never later than
MAX_WAIT_SECONDS after the first while requests keep coming. A sweep that
translates twenty rows, or a batch of price changes, is one refresh, not one
per row — each refresh makes the next visitors redraw their pages.

Off unless both STOREFRONT_REVALIDATE_URL and REVALIDATE_SECRET are set. A
failed call is logged and dropped: the change still shows when the cache
window runs out.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

#: Quiet time after the last request before the refresh goes out.
QUIET_SECONDS = 60.0
#: The longest a request waits while others keep arriving.
MAX_WAIT_SECONDS = 600.0

_first: float | None = None  # loop time of the oldest request not yet sent
_last: float | None = None  # loop time of the newest
_task: asyncio.Task | None = None


def enabled() -> bool:
    return bool(settings.STOREFRONT_REVALIDATE_URL and settings.REVALIDATE_SECRET)


def request_refresh() -> None:
    """Ask for a refresh; it goes out once the requests settle."""
    global _first, _last, _task
    if not enabled():
        return
    loop = asyncio.get_running_loop()
    now = loop.time()
    if _first is None:
        _first = now
    _last = now
    if _task is None or _task.done():
        _task = loop.create_task(_run())


async def _run() -> None:
    global _first, _last
    loop = asyncio.get_running_loop()
    # Requests that arrive while a refresh is on its way start the wait again.
    while _first is not None and _last is not None:
        delay = min(_last + QUIET_SECONDS, _first + MAX_WAIT_SECONDS) - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
            continue
        _first = _last = None
        await refresh()


async def flush() -> None:
    """Send a waiting refresh now (the API shutting down: a deploy)."""
    global _first, _last
    if _first is None:
        return
    _first = _last = None
    if _task is not None and not _task.done():
        _task.cancel()
    await refresh()


async def refresh() -> None:
    """One call to the storefront (replaced in the tests)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                settings.STOREFRONT_REVALIDATE_URL, headers={"X-Revalidate-Secret": settings.REVALIDATE_SECRET}
            )
        if response.status_code == 200:
            logger.info("Storefront cache dropped")
        else:
            logger.warning("Storefront revalidation refused (%d)", response.status_code)
    except httpx.HTTPError:
        logger.warning("Storefront revalidation failed", exc_info=True)
