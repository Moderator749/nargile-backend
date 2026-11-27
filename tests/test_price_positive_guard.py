"""No price of 0 or below (2026-11-27): an empty cell is "not sold to this
group", never "free". Every writer refuses one (pricing_engine.require_positive),
the table's CHECK constraint backs that up, and checkout refuses a line whose
resolved unit price is 0 or below rather than charging nothing for it."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import CurrencyType, ProductVariant, VariantPrice
from app.models.user import SegmentType, User
from app.services import checkout_engine
from app.services.pricing_engine import NonPositivePrice, require_positive
from app.services.pricing_sheet import ApplyError, write_prices
from tests.conftest import auth_header


@pytest.mark.parametrize("excl,incl", [("0", "0"), ("0", "1.20"), ("1.00", "0"), ("-1", "-1.20"), (None, "1.20")])
def test_a_zero_or_negative_price_is_refused(excl, incl):
    with pytest.raises(NonPositivePrice):
        require_positive(Decimal(excl) if excl else None, Decimal(incl), what="Kafe")


def test_a_positive_price_passes():
    require_positive(Decimal("0.01"), Decimal("0.01"), what="Kafe")


@pytest.mark.parametrize("incl", ["0", "-5"])
async def test_worksheets_and_imports_cannot_write_a_zero_price(
    db_session: AsyncSession, catalog, segments, incl: str
):
    normal_v, _ = catalog
    cafe_id = segments[SegmentType.CAFE].id
    with pytest.raises(ApplyError, match="0 veya negatif"):
        await write_prices(
            db_session, [normal_v], [(cafe_id, CurrencyType.TRY, Decimal(incl), Decimal(incl))],
            Decimal("20"), retail_id=segments[SegmentType.RETAIL].id,
        )


async def test_the_database_refuses_a_zero_price(db_session: AsyncSession, catalog, segments):
    normal_v, _ = catalog
    db_session.add(VariantPrice(
        variant_id=normal_v.id, segment_id=segments[SegmentType.CAFE].id, currency="TRY",
        price_excl_tax=Decimal("0"), price_incl_tax=Decimal("0"), vat_rate=Decimal("20"),
    ))
    with pytest.raises(IntegrityError, match="ck_variant_prices_positive"):
        await db_session.flush()
    await db_session.rollback()


async def test_checkout_refuses_a_line_priced_at_zero(
    client: AsyncClient, admin_user: User, catalog, monkeypatch
):
    """A row that somehow reads 0 (the constraint makes it unwritable now) is
    refused at the cart, never charged as a free item."""
    normal_v, _ = catalog
    zero = SimpleNamespace(
        currency=CurrencyType.TRY, price_excl_tax=Decimal("0"), price_incl_tax=Decimal("0"),
        vat_rate=Decimal("20"),
    )
    monkeypatch.setattr(checkout_engine, "pick_price_row", lambda prices, segment_id: zero)
    resp = await client.post(
        "/api/v1/orders/calculate-cart",
        headers=auth_header(admin_user),
        json={"payment_method": "BANK_TRANSFER", "items": [{"variant_id": str(normal_v.id), "quantity": 1}]},
    )
    assert 400 <= resp.status_code < 500, resp.text
    assert "geçerli bir fiyat yok" in resp.text


async def test_checkout_still_prices_a_normal_line(client: AsyncClient, admin_user: User, catalog):
    normal_v, _ = catalog
    resp = await client.post(
        "/api/v1/orders/calculate-cart",
        headers=auth_header(admin_user),
        json={"payment_method": "BANK_TRANSFER", "items": [{"variant_id": str(normal_v.id), "quantity": 1}]},
    )
    assert resp.status_code == 200 and Decimal(resp.json()["subtotal"]) == Decimal("5000.00")
