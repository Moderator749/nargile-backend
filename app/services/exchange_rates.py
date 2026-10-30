"""Daily TRY exchange rates from the Central Bank of Türkiye (TCMB).

A price entered in USD or EUR is sold at the day's rate. The feed is cached
in-process, and a stale copy is served for a while if TCMB is unreachable.

The last bulletin read is also kept in the database (shop_settings), so a
restart — every deploy is one — that meets TCMB unreachable still has it:
otherwise every foreign-priced product would go unpriced until TCMB answered
again. It is served on the same terms as the in-process copy (up to
STALE_OK_FOR_S after it was read).
"""

import asyncio
import json
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.database import async_session
from app.services.shop_settings import read_setting, save_setting

logger = logging.getLogger(__name__)

TCMB_URL = "https://www.tcmb.gov.tr/kurlar/today.xml"
CURRENCIES = ("USD", "EUR")
FRESH_FOR_S = 60 * 60  # TCMB publishes once a day; hourly refresh is plenty
STALE_OK_FOR_S = 48 * 60 * 60  # weekends/holidays: no new bulletin, keep the last one
TIMEOUT_S = 5.0
#: The shop_settings row holding the last bulletin read.
SNAPSHOT_KEY = "tcmb_rates_last"


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
#: Where the snapshot is kept; tests point it at their own database.
_sessions: async_sessionmaker = async_session


async def _fetch() -> Rates:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.get(TCMB_URL)
        resp.raise_for_status()
    return parse_tcmb(resp.text)


def _dump(rates: Rates, fetched_at: datetime) -> str:
    return json.dumps(
        {
            "bulletin_date": rates.bulletin_date.isoformat(),
            "try_per_unit": {code: str(rate) for code, rate in rates.try_per_unit.items()},
            "fetched_at": fetched_at.isoformat(),
        }
    )


def _load(text: str) -> tuple[Rates, datetime]:
    data = json.loads(text)
    rates = Rates(
        bulletin_date=date.fromisoformat(data["bulletin_date"]),
        try_per_unit={code: Decimal(rate) for code, rate in data["try_per_unit"].items()},
    )
    return rates, datetime.fromisoformat(data["fetched_at"])


async def _save_snapshot(rates: Rates) -> None:
    """Keep this bulletin for the next restart. Never fails a price lookup."""
    try:
        async with _sessions() as db:
            await save_setting(db, SNAPSHOT_KEY, _dump(rates, datetime.now(timezone.utc)))
            await db.commit()
    except Exception:  # noqa: BLE001 — the rates are served whatever happens here
        logger.warning("Could not store the TCMB rates snapshot", exc_info=True)


async def _read_snapshot() -> tuple[float, Rates] | None:
    """The stored bulletin as a cache entry (its age carried over), or None."""
    try:
        async with _sessions() as db:
            text = await read_setting(db, SNAPSHOT_KEY)
        if text is None:
            return None
        rates, fetched_at = _load(text)
    except Exception:  # noqa: BLE001
        logger.warning("Could not read the TCMB rates snapshot", exc_info=True)
        return None
    age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
    return time.monotonic() - max(age, 0), rates


async def get_rates() -> Rates:
    global _cache
    async with _lock:
        now = time.monotonic()
        if _cache and now - _cache[0] < FRESH_FOR_S:
            return _cache[1]
        try:
            rates = await _fetch()
        except (httpx.HTTPError, ET.ParseError, RatesUnavailable) as exc:
            # Just restarted: the last bulletin read before it, from the database.
            if _cache is None:
                _cache = await _read_snapshot()
            if _cache and now - _cache[0] < STALE_OK_FOR_S:
                logger.warning("TCMB unreachable; serving the rates of %s", _cache[1].bulletin_date)
                return _cache[1]
            raise RatesUnavailable("TCMB rates unavailable") from exc
        # Stored once per new bulletin (and once after a restart), not on every hourly read.
        if _cache is None or _cache[1] != rates:
            await _save_snapshot(rates)
        _cache = (now, rates)
        return rates
