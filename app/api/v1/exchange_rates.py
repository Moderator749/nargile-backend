"""Display-only currency rates (prices are always charged in TRY)."""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from app.services.exchange_rates import RatesUnavailable, get_rates

router = APIRouter(tags=["exchange-rates"])


class ExchangeRatesRead(BaseModel):
    base: str = "TRY"
    source: str = "TCMB"
    bulletin_date: date
    # TRY per 1 unit: an amount in TRY / rate = amount in that currency.
    rates: dict[str, Decimal]


@router.get("/exchange-rates", response_model=ExchangeRatesRead)
async def exchange_rates(response: Response):
    try:
        rates = await get_rates()
    except RatesUnavailable:
        raise HTTPException(status_code=503, detail="Exchange rates unavailable")
    response.headers["Cache-Control"] = "public, max-age=3600"
    return ExchangeRatesRead(bulletin_date=rates.bulletin_date, rates=rates.try_per_unit)
