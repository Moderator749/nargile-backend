"""TCMB exchange-rate parsing, the display-only /exchange-rates endpoint, and the
last bulletin kept across a restart (no network)."""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1 import exchange_rates as api
from app.models.settings import ShopSetting
from app.services import exchange_rates as service
from app.services.exchange_rates import Rates, RatesUnavailable, parse_tcmb
from app.services.shop_settings import read_setting

SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<Tarih_Date Tarih="05.10.2026" Date="10/05/2026" Bulten_No="2026/187">
  <Currency Kod="USD" CurrencyCode="USD"><Unit>1</Unit><ForexSelling>49.1616</ForexSelling></Currency>
  <Currency Kod="JPY" CurrencyCode="JPY"><Unit>100</Unit><ForexSelling>32.5000</ForexSelling></Currency>
  <Currency Kod="EUR" CurrencyCode="EUR"><Unit>1</Unit><ForexSelling>57.0012</ForexSelling></Currency>
</Tarih_Date>"""


def test_parse_tcmb():
    rates = parse_tcmb(SAMPLE)
    assert rates.bulletin_date == date(2026, 10, 5)
    assert rates.try_per_unit == {"USD": Decimal("49.1616"), "EUR": Decimal("57.0012")}


def test_parse_tcmb_divides_by_unit():
    xml = SAMPLE.replace("<Unit>1</Unit><ForexSelling>49.1616", "<Unit>10</Unit><ForexSelling>491.616")
    assert parse_tcmb(xml).try_per_unit["USD"] == Decimal("49.1616")


def test_parse_tcmb_requires_all_currencies():
    with pytest.raises(RatesUnavailable):
        parse_tcmb(SAMPLE.replace('CurrencyCode="EUR"', 'CurrencyCode="XXX"'))


async def test_endpoint_returns_rates(client: AsyncClient, monkeypatch):
    async def fake():
        return Rates(bulletin_date=date(2026, 10, 5), try_per_unit={"USD": Decimal("49.1616"), "EUR": Decimal("57.0012")})

    monkeypatch.setattr(api, "get_rates", fake)
    resp = await client.get("/api/v1/exchange-rates")
    assert resp.status_code == 200
    assert resp.json() == {
        "base": "TRY",
        "source": "TCMB",
        "bulletin_date": "2026-10-05",
        "rates": {"USD": "49.1616", "EUR": "57.0012"},
    }


async def test_endpoint_503_when_unavailable(client: AsyncClient, monkeypatch):
    async def down():
        raise RatesUnavailable("down")

    monkeypatch.setattr(api, "get_rates", down)
    assert (await client.get("/api/v1/exchange-rates")).status_code == 503


# ── The last bulletin, kept across a restart ─────────────────────────────────

TODAY = Rates(bulletin_date=date(2026, 10, 30), try_per_unit={"USD": Decimal("49.5000"), "EUR": Decimal("57.4000")})


@pytest.fixture
async def snapshots(db_session: AsyncSession, monkeypatch):
    """The service's cache empty (a fresh process) and its snapshot on the test database."""
    monkeypatch.setattr(service, "_sessions", async_sessionmaker(db_session.bind, expire_on_commit=False))
    monkeypatch.setattr(service, "_cache", None)
    await db_session.execute(delete(ShopSetting).where(ShopSetting.key == service.SNAPSHOT_KEY))
    await db_session.commit()
    yield db_session
    await db_session.execute(delete(ShopSetting).where(ShopSetting.key == service.SNAPSHOT_KEY))
    await db_session.commit()


def _tcmb(rates: Rates | None):
    async def fetch():
        if rates is None:
            raise httpx.ConnectError("TCMB unreachable")
        return rates

    return fetch


async def test_a_bulletin_read_is_kept_in_the_database(snapshots: AsyncSession, monkeypatch):
    monkeypatch.setattr(service, "_fetch", _tcmb(TODAY))
    assert await service.get_rates() == TODAY
    stored = json.loads(await read_setting(snapshots, service.SNAPSHOT_KEY))
    assert stored["bulletin_date"] == "2026-10-30"
    assert stored["try_per_unit"] == {"USD": "49.5000", "EUR": "57.4000"}


async def test_after_a_restart_with_tcmb_down_the_kept_bulletin_is_served(snapshots: AsyncSession, monkeypatch):
    monkeypatch.setattr(service, "_fetch", _tcmb(TODAY))
    await service.get_rates()
    # The restart: the process forgets, TCMB does not answer.
    monkeypatch.setattr(service, "_cache", None)
    monkeypatch.setattr(service, "_fetch", _tcmb(None))
    assert await service.get_rates() == TODAY


async def test_a_kept_bulletin_too_old_is_not_served(snapshots: AsyncSession, monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(seconds=service.STALE_OK_FOR_S + 60)
    snapshots.add(ShopSetting(key=service.SNAPSHOT_KEY, value=service._dump(TODAY, old)))
    await snapshots.commit()
    monkeypatch.setattr(service, "_fetch", _tcmb(None))
    with pytest.raises(RatesUnavailable):
        await service.get_rates()


async def test_no_kept_bulletin_and_tcmb_down_is_unavailable(snapshots: AsyncSession, monkeypatch):
    monkeypatch.setattr(service, "_fetch", _tcmb(None))
    with pytest.raises(RatesUnavailable):
        await service.get_rates()


async def test_a_database_failure_never_costs_the_rates(snapshots: AsyncSession, monkeypatch):
    def broken():
        raise RuntimeError("database down")

    monkeypatch.setattr(service, "_sessions", broken)
    monkeypatch.setattr(service, "_fetch", _tcmb(TODAY))
    assert await service.get_rates() == TODAY
