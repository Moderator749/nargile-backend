"""Daily TRY exchange rates from the Central Bank of Türkiye (TCMB).

Display only: prices are stored and charged in TRY; converted amounts are shown
as approximations. The feed is cached in-process, and a stale copy is served for
a while if TCMB is unreachable.
"""

import asyncio
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import httpx

TCMB_URL = "https://www.tcmb.gov.tr/kurlar/today.xml"
CURRENCIES = ("USD", "EUR")
FRESH_FOR_S = 60 * 60  # TCMB publishes once a day; hourly refresh is plenty
STALE_OK_FOR_S = 48 * 60 * 60  # weekends/holidays: no new bulletin, keep the last one
TIMEOUT_S = 5.0


class RatesUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Rates:
    bulletin_date: date
    # TRY per 1 unit of each currency (TCMB "ForexSelling").
    try_per_unit: dict[str, Decimal]


def parse_tcmb(xml_text: str) -> Rates:
    """Parse TCMB today.xml into TRY-per-unit rates for CURRENCIES."""
    root = ET.fromstring(xml_text)
    try:
        bulletin = datetime.strptime(root.attrib["Tarih"], "%d.%m.%Y").date()
    except (KeyError, ValueError) as exc:
        raise RatesUnavailable("TCMB bulletin date missing") from exc

    rates: dict[str, Decimal] = {}
    for node in root.iter("Currency"):
        code = node.attrib.get("CurrencyCode")
        if code not in CURRENCIES:
            continue
        try:
            unit = Decimal(node.findtext("Unit") or "1")
            selling = Decimal(node.findtext("ForexSelling") or "")
        except InvalidOperation:
            continue
        if unit > 0 and selling > 0:
            rates[code] = (selling / unit).quantize(Decimal("0.0001"))

    missing = set(CURRENCIES) - rates.keys()
    if missing:
        raise RatesUnavailable(f"TCMB feed lacks {', '.join(sorted(missing))}")
    return Rates(bulletin_date=bulletin, try_per_unit=rates)


_cache: tuple[float, Rates] | None = None
_lock = asyncio.Lock()


async def _fetch() -> Rates:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.get(TCMB_URL)
        resp.raise_for_status()
    return parse_tcmb(resp.text)


async def get_rates() -> Rates:
    global _cache
    async with _lock:
        now = time.monotonic()
        if _cache and now - _cache[0] < FRESH_FOR_S:
            return _cache[1]
        try:
            rates = await _fetch()
        except (httpx.HTTPError, ET.ParseError, RatesUnavailable) as exc:
            if _cache and now - _cache[0] < STALE_OK_FOR_S:
                return _cache[1]
            raise RatesUnavailable("TCMB rates unavailable") from exc
        _cache = (now, rates)
        return rates
