"""TRY prices in whole lira: a converted price, a computed one, and the
stored ones the one-off script rounds (1163.86 → 1164.00, rounded up)."""

import importlib.util
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1 import products as products_api
from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice, WarehouseType
from app.models.user import CustomerSegment, SegmentType, User
from app.services import checkout_engine
from app.services.exchange_rates import Rates
from app.services.pricing_engine import row_in_try, to_try, whole_lira
from tests.conftest import ADDRESS, CONSENTS, auth_header

# $19.40 at 60.0 TRY/USD = 1164.00 exactly; at 59.995 = 1163.903 → 1164.00.
RATES = Rates(bulletin_date=date.today(), try_per_unit={"USD": Decimal("59.995"), "EUR": Decimal("65")})


async def _rates():
    return RATES


@pytest.mark.parametrize(
    ("amount", "whole"),
    [("1163.86", "1164.00"), ("1164.00", "1164.00"), ("0.01", "1.00"), ("999.999", "1000.00"), ("0", "0.00")],
)
def test_whole_lira_rounds_up_and_keeps_whole_amounts(amount: str, whole: str):
    assert whole_lira(Decimal(amount)) == Decimal(whole)


def test_a_converted_price_is_whole_lira_and_lira_stays_as_typed():
    assert to_try(Decimal("19.40"), "USD", RATES) == Decimal("1164.00")
    assert to_try(Decimal("1163.86"), "TRY", RATES) == Decimal("1163.86")  # TRY is charged as typed
    assert to_try(Decimal("19.40"), "USD", None) is None


def test_the_figure_before_vat_follows_the_whole_price():
    row = VariantPrice(currency="USD", price_excl_tax=Decimal("16.17"), price_incl_tax=Decimal("19.40"), vat_rate=Decimal("20.00"))
    # 1164 after VAT is 970.00 before it — not 16.17 × 59.995 = 970.12 rounded on its own.
    assert row_in_try(row, RATES) == (Decimal("970.00"), Decimal("1164.00"))


@pytest.fixture
async def dollar_product(db_session: AsyncSession, segments: dict[SegmentType, CustomerSegment]):
    """A published product sold at $19.40 incl. 20% VAT to RETAIL, no TRY row."""
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"WL-{suffix}", name=f"Dolar Lüle {suffix}", slug=f"dolar-lule-{suffix}", tags=[], is_published=True)
    db_session.add(product)
    await db_session.flush()
    variant = ProductVariant(product_id=product.id, sku=f"WL-{suffix}")
    db_session.add(variant)
    await db_session.flush()
    db_session.add(VariantPrice(
        variant_id=variant.id, segment_id=segments[SegmentType.RETAIL].id, currency="USD",
        price_excl_tax=Decimal("16.17"), price_incl_tax=Decimal("19.40"), vat_rate=Decimal("20.00"),
    ))
    db_session.add(Inventory(variant_id=variant.id, warehouse_type=WarehouseType.RETAIL_STOCK, quantity=20))
    await db_session.commit()
    return product, variant


async def test_the_storefront_shows_a_dollar_price_in_whole_lira(client: AsyncClient, dollar_product, monkeypatch):
    monkeypatch.setattr(products_api, "get_rates", _rates)
    product, _ = dollar_product
    listed = (await client.get("/api/v1/products", params={"ids": str(product.id)})).json()
    price = listed[0]["variants"][0]["price"]
    assert (price["currency"], price["price_incl_tax"], price["price_excl_tax"]) == ("TRY", "1164.00", "970.00")
    # The dollar figure it came from is still shown beside it, as entered.
    assert listed[0]["variants"][0]["origin_price"]["price_incl_tax"] == "19.40"


async def test_checkout_charges_the_whole_lira_price(
    client: AsyncClient, customer_user: User, dollar_product, monkeypatch
):
    monkeypatch.setattr(checkout_engine, "get_rates", _rates)
    _, variant = dollar_product
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(customer_user),
        json={
            **CONSENTS,
            "payment_method": "BANK_TRANSFER",
            "items": [{"variant_id": str(variant.id), "quantity": 2}],
            "shipping_address": ADDRESS,
            "shipping_consent": True,
        },
    )
    assert placed.status_code == 201, placed.text
    line = placed.json()["items"][0]
    assert Decimal(line["unit_price"]) == Decimal("1164.00")


def _script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "round_try_prices.py"
    spec = importlib.util.spec_from_file_location("round_try_prices", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_script_rounds_stored_lira_prices_once(
    db_session: AsyncSession, catalog, segments: dict[SegmentType, CustomerSegment], tmp_path, monkeypatch
):
    normal_v, _ = catalog
    variant_id = normal_v.id
    dealer, retail_id = segments[SegmentType.DEALER].id, segments[SegmentType.RETAIL].id
    row = VariantPrice(
        variant_id=variant_id, segment_id=dealer, currency="TRY",
        price_excl_tax=Decimal("969.88"), price_incl_tax=Decimal("1163.86"), vat_rate=Decimal("20.00"),
    )
    db_session.add(row)
    await db_session.commit()
    row_id = row.id

    script = _script()
    monkeypatch.setattr(script, "REPORTS", tmp_path)
    # The test's own connection: the app's engine may hold one from another test's loop.
    monkeypatch.setattr(script, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))

    dry = await script.run(apply=False, refresh=False)
    assert any(r["id"] == row_id for r in dry["variant_prices"])
    db_session.expire_all()
    assert (await db_session.get(VariantPrice, row_id)).price_incl_tax == Decimal("1163.86")  # nothing written

    await script.run(apply=True, refresh=False)
    db_session.expire_all()
    stored = await db_session.get(VariantPrice, row_id)
    assert (stored.price_excl_tax, stored.price_incl_tax) == (Decimal("970.00"), Decimal("1164.00"))
    # The whole retail row beside it was not touched, and the old figures were saved.
    retail = (await db_session.scalars(
        select(VariantPrice).where(VariantPrice.variant_id == variant_id, VariantPrice.segment_id == retail_id)
    )).one()
    assert retail.price_incl_tax == Decimal("5000.00") and retail.price_excl_tax == Decimal("4237.29")
    saved = list(tmp_path.glob("round-try-prices-*.json"))
    assert len(saved) == 1 and '"old_incl": "1163.86"' in saved[0].read_text()

    again = await script.run(apply=True, refresh=False)
    assert not any(r["id"] == row_id for r in again["variant_prices"])
