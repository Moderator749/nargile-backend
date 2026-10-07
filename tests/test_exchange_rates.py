"""TCMB exchange-rate parsing and the display-only /exchange-rates endpoint (no network)."""

from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient

from app.api.v1 import exchange_rates as api
from app.services.exchange_rates import Rates, RatesUnavailable, parse_tcmb

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
