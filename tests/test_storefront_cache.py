"""Storefront refreshes asked for by the backend are gathered: one call a quiet
moment after the last request, never later than the longest wait after the
first, and none at all without the storefront's address and secret."""

import asyncio

import pytest

from app.core.config import settings
from app.services import auto_translate, storefront_cache


@pytest.fixture
def calls(monkeypatch) -> list[int]:
    """Refreshing on, with short waits and a stand-in for the POST."""
    made: list[int] = []

    async def record():
        made.append(1)

    monkeypatch.setattr(settings, "STOREFRONT_REVALIDATE_URL", "http://localhost:3000/api/revalidate")
    monkeypatch.setattr(settings, "REVALIDATE_SECRET", "s" * 64)
    monkeypatch.setattr(storefront_cache, "QUIET_SECONDS", 0.05)
    monkeypatch.setattr(storefront_cache, "MAX_WAIT_SECONDS", 10)
    monkeypatch.setattr(storefront_cache, "refresh", record)
    yield made
    if storefront_cache._task is not None:
        storefront_cache._task.cancel()
    storefront_cache._first = storefront_cache._last = None


async def test_a_burst_of_requests_is_one_refresh(calls):
    for _ in range(200):  # a bulk import's worth
        storefront_cache.request_refresh()
    await asyncio.sleep(0.02)
    assert calls == []  # still gathering
    await asyncio.sleep(0.1)
    assert calls == [1]


async def test_requests_that_keep_coming_wait_no_longer_than_the_cap(calls, monkeypatch):
    monkeypatch.setattr(storefront_cache, "MAX_WAIT_SECONDS", 0.15)
    for _ in range(8):  # every 25 ms for 200 ms: never quiet for 50 ms
        storefront_cache.request_refresh()
        await asyncio.sleep(0.025)
    # Sent once the cap ran out (at 150 ms), though requests were still coming;
    # the ones after it wait for their own quiet moment (about 250 ms).
    assert len(calls) == 1
    await asyncio.sleep(0.15)
    assert len(calls) == 2


async def test_a_request_during_a_refresh_gets_one_of_its_own(calls, monkeypatch):
    async def slow():
        calls.append(1)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(storefront_cache, "refresh", slow)
    storefront_cache.request_refresh()
    await asyncio.sleep(0.07)  # the first refresh is on its way
    storefront_cache.request_refresh()
    await asyncio.sleep(0.2)
    assert calls == [1, 1]


async def test_flush_sends_a_waiting_refresh_now(calls):
    storefront_cache.request_refresh()
    await storefront_cache.flush()
    assert calls == [1]
    await asyncio.sleep(0.1)
    assert calls == [1]  # not again when the wait would have ended
    await storefront_cache.flush()
    assert calls == [1]  # nothing waiting: nothing sent


async def test_nothing_without_the_address_or_the_secret(calls, monkeypatch):
    monkeypatch.setattr(settings, "REVALIDATE_SECRET", None)
    storefront_cache.request_refresh()
    monkeypatch.setattr(settings, "REVALIDATE_SECRET", "s" * 64)
    monkeypatch.setattr(settings, "STOREFRONT_REVALIDATE_URL", None)
    storefront_cache.request_refresh()
    await asyncio.sleep(0.1)
    assert calls == []


async def test_translations_ask_through_the_gathering(calls):
    for _ in range(20):  # one sweep: twenty rows translated
        auto_translate._refresh_storefront()
    await asyncio.sleep(0.1)
    assert calls == [1]
