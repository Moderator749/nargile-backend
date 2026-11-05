"""A product priced only in dollars or euros, in the two admin screens that
once looked for a TRY row alone: the flash sale's regular price, and the VAT
rate on the order sheet."""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import admin_promotions
from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice, WarehouseType
from app.models.order import OrderItem
from app.models.promotion import FlashSale
from app.models.user import CustomerSegment, SegmentType, User
from app.services import checkout_engine
from app.services.exchange_rates import Rates, RatesUnavailable
from tests.conftest import ADDRESS, CONSENTS, auth_header

RATES = Rates(bulletin_date=date.today(), try_per_unit={"USD": Decimal("40"), "EUR": Decimal("45")})


async def _rates():
    return RATES


async def _no_rates():
    raise RatesUnavailable


@pytest.fixture
async def dollar_variant(db_session: AsyncSession, segments: dict[SegmentType, CustomerSegment]):
    """$10.00 incl. 20% VAT to RETAIL, no TRY row: 400 TRY at 40 TRY/USD."""
    suffix = uuid.uuid4().hex[:6].upper()
    product = Product(base_code=f"USD-{suffix}", name="Dövizli Nargile", tags=[])
    db_session.add(product)
    await db_session.flush()
    variant = ProductVariant(product_id=product.id, sku=f"USD-{suffix}")
    db_session.add(variant)
    await db_session.flush()
    db_session.add(VariantPrice(
        variant_id=variant.id, segment_id=segments[SegmentType.RETAIL].id, currency="USD",
        price_excl_tax=Decimal("8.33"), price_incl_tax=Decimal("10.00"), vat_rate=Decimal("20.00"),
    ))
    db_session.add(Inventory(variant_id=variant.id, warehouse_type=WarehouseType.RETAIL_STOCK, quantity=50))
    await db_session.commit()
    variant_id, sku = variant.id, variant.sku
    yield variant
    await db_session.rollback()
    await db_session.execute(delete(FlashSale).where(FlashSale.variant_id == variant_id))
    await db_session.commit()


def _sale(sku: str, price: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "sku": sku,
        "sale_price_incl_tax": price,
        "starts_at": (now - timedelta(minutes=1)).isoformat(),
        "ends_at": (now + timedelta(hours=6)).isoformat(),
    }


# ── Flash sale: the regular price in lira, as checkout charges it ─────────────


async def test_a_dollar_priced_variant_gets_a_flash_sale_against_its_lira_price(
    client: AsyncClient, admin_user: User, dollar_variant, monkeypatch
):
    monkeypatch.setattr(admin_promotions, "get_rates", _rates)
    h = auth_header(admin_user)

    not_a_sale = await client.post("/api/v1/admin/flash-sales", headers=h, json=_sale(dollar_variant.sku, "400.00"))
    assert not_a_sale.status_code == 422 and "400.00" in not_a_sale.json()["detail"]

    created = await client.post("/api/v1/admin/flash-sales", headers=h, json=_sale(dollar_variant.sku, "349.00"))
    assert created.status_code == 201, created.text
    assert created.json()["regular_price_incl_tax"] == "400.00"

    listed = (await client.get("/api/v1/admin/flash-sales", headers=h)).json()
    assert next(s for s in listed if s["sku"] == dollar_variant.sku)["regular_price_incl_tax"] == "400.00"


async def test_without_the_rate_a_dollar_sale_is_not_saved_unchecked(
    client: AsyncClient, admin_user: User, dollar_variant, monkeypatch
):
    h = auth_header(admin_user)
    monkeypatch.setattr(admin_promotions, "get_rates", _rates)
    saved = await client.post("/api/v1/admin/flash-sales", headers=h, json=_sale(dollar_variant.sku, "349.00"))
    assert saved.status_code == 201

    monkeypatch.setattr(admin_promotions, "get_rates", _no_rates)
    edited = await client.put(f"/api/v1/admin/flash-sales/{saved.json()['id']}", headers=h, json=_sale(dollar_variant.sku, "299.00"))
    assert edited.status_code == 503 and "USD kuru" in edited.json()["detail"]
    # The list still loads; the regular price is unknown for now, not wrong.
    listed = await client.get("/api/v1/admin/flash-sales", headers=h)
    assert listed.status_code == 200
    assert next(s for s in listed.json() if s["sku"] == dollar_variant.sku)["regular_price_incl_tax"] is None


async def test_a_lira_priced_variant_never_waits_on_the_rates(
    client: AsyncClient, admin_user: User, catalog, monkeypatch
):
    async def must_not_fetch():
        raise AssertionError("rates fetched for a TRY-priced variant")

    monkeypatch.setattr(admin_promotions, "get_rates", must_not_fetch)
    normal_v, _ = catalog
    h = auth_header(admin_user)
    created = await client.post("/api/v1/admin/flash-sales", headers=h, json=_sale(normal_v.sku, "3990.00"))
    assert created.status_code == 201, created.text
    assert created.json()["regular_price_incl_tax"] == "5000.00"
    assert (await client.get("/api/v1/admin/flash-sales", headers=h)).status_code == 200
    assert (await client.delete(f"/api/v1/admin/flash-sales/{created.json()['id']}", headers=h)).status_code == 204


# ── Order sheet: the VAT rate of a line sold in dollars ──────────────────────


async def test_the_order_sheet_gives_a_dollar_lines_vat(
    client: AsyncClient, admin_user: User, customer_user: User, dollar_variant, db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(checkout_engine, "get_rates", _rates)
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(customer_user),
        json={
            **CONSENTS,
            "payment_method": "BANK_TRANSFER",
            "items": [{"variant_id": str(dollar_variant.id), "quantity": 1}],
            "shipping_address": ADDRESS,
            "shipping_consent": True,
        },
    )
    assert placed.status_code == 201, placed.text
    order_id = placed.json()["id"]

    async def sheet_line() -> dict:
        listed = (await client.get("/api/v1/admin/orders", headers=auth_header(admin_user))).json()
        return next(o for o in listed if o["id"] == order_id)["items"][0]

    # The rate frozen at checkout, from the dollar row it was sold at.
    line = await sheet_line()
    assert Decimal(line["unit_price"]) == Decimal("400.00") and Decimal(line["vat_rate"]) == Decimal("20.00")

    # A line from before the rate was frozen: the catalogue's, from the dollar row.
    await db_session.execute(update(OrderItem).where(OrderItem.order_id == uuid.UUID(order_id)).values(vat_rate=None))
    await db_session.commit()
    assert Decimal((await sheet_line())["vat_rate"]) == Decimal("20.00")
