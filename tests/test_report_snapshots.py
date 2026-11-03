"""What the reports stand on: a product's cost, and each sale as it was made.

Covers:
- Applying a worksheet writes its cost to the product: before VAT, in the
  sheet's currency (an INCL sheet's cost loses the retail VAT rate)
- Checkout freezes the buyer's price list on the order and, on each line, the
  VAT rate of the price and the unit's cost in TRY before VAT
- A USD/EUR cost is converted at rates already in memory and never sends a
  checkout to TCMB: without them the line's cost is simply unknown
- An account in no group is recorded as Perakende, the list it was priced from
- The backfill gives each product its latest applied worksheet's cost, once
"""

import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import CurrencyType, Inventory, Product, ProductVariant, VariantPrice
from app.models.order import Order, OrderItem
from app.models.pricing import PricingRecord
from app.models.user import CustomerSegment, SegmentType, User, UserRole
from app.services import exchange_rates
from app.services.checkout_engine import CENTRAL_STOCK
from app.services.exchange_rates import Rates
from scripts.backfill_product_costs import backfill
from tests.conftest import ADDRESS, CONSENTS, auth_header

pytestmark = pytest.mark.asyncio

PRICE = Decimal("6000.00")  # over the free-shipping threshold


def _sheet(code: str, **overrides) -> dict:
    sheet = {
        "product_code": code,
        "product_name": "Maliyet Testi",
        "vat_mode": "EXCL",
        "currency": "TRY",
        "cost": "1000",
        "columns": [{"label": "Bayi Fiyatı", "segment": "DEALER", "percent": "15"}],
        "retail": {"percent": "20", "vat_rate": "20"},
    }
    sheet.update(overrides)
    return sheet


async def _variant(
    db: AsyncSession, segments: dict[SegmentType, CustomerSegment], *, cost: str | None, currency: str = "TRY"
) -> ProductVariant:
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"MLY-{suffix}", name=f"Maliyet {suffix}", tags=[])
    db.add(product)
    await db.flush()
    variant = ProductVariant(
        product_id=product.id, sku=f"MLY-{suffix}",
        cost_price=Decimal(cost) if cost is not None else None, cost_currency=currency,
    )
    db.add(variant)
    await db.flush()
    for code in SegmentType:
        db.add(VariantPrice(
            variant_id=variant.id, segment_id=segments[code].id, currency="TRY",
            price_excl_tax=(PRICE / Decimal("1.20")).quantize(Decimal("0.01")),
            price_incl_tax=PRICE, vat_rate=Decimal("20.00"),
        ))
    db.add(Inventory(variant_id=variant.id, warehouse_type=CENTRAL_STOCK, quantity=20))
    await db.commit()
    return variant


async def _buyer(db: AsyncSession, segment_id: int | None) -> User:
    user = User(
        email=f"rapor-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        full_name="Rapor Alıcısı",
        phone="0532 111 22 33",
        role=UserRole.CUSTOMER,
        segment_id=segment_id,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _order(client: AsyncClient, db: AsyncSession, user: User, variant: ProductVariant) -> Order:
    resp = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(user),
        json={
            **CONSENTS,
            "payment_method": "CREDIT_CARD",
            "items": [{"variant_id": str(variant.id), "quantity": 2}],
            "shipping_method": "YURTICI_DOOR",
            "shipping_consent": True,
            "shipping_address": ADDRESS,
        },
    )
    assert resp.status_code == 201, resp.text
    order = await db.scalar(
        select(Order).where(Order.id == uuid.UUID(resp.json()["id"])).execution_options(populate_existing=True)
    )
    return order


async def _line(db: AsyncSession, order: Order) -> OrderItem:
    return await db.scalar(
        select(OrderItem).where(OrderItem.order_id == order.id).execution_options(populate_existing=True)
    )


async def _cost(db: AsyncSession, variant_id) -> tuple[Decimal | None, str]:
    row = (await db.execute(
        select(ProductVariant.cost_price, ProductVariant.cost_currency).where(ProductVariant.id == variant_id)
    )).one()
    return row[0], CurrencyType(row[1]).value


# ── The worksheet writes the cost ────────────────────────────────────────────


async def test_applying_a_worksheet_writes_its_cost_before_vat(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, segments
):
    variant = await _variant(db_session, segments, cost=None)
    code = variant.sku
    h = auth_header(admin_user)

    resp = await client.post(
        "/api/v1/admin/pricing/apply-to-product", headers=h, json={"file_name": "Maliyet", "sheet": _sheet(code)}
    )
    assert resp.status_code == 200, resp.text
    assert await _cost(db_session, variant.id) == (Decimal("1000.0000"), "TRY")
    assert resp.json()["record"]["applied_summary"]["cost"] == "1000.0000"

    # A VAT-inclusive USD sheet: 120 with %20 inside is 100 before VAT, in USD.
    sheet = _sheet(code, vat_mode="INCL", currency="USD", cost="120", usd_rate="40")
    resp = await client.post("/api/v1/admin/pricing/apply-to-product", headers=h, json={"file_name": "Maliyet", "sheet": sheet})
    assert resp.status_code == 200, resp.text
    assert await _cost(db_session, variant.id) == (Decimal("100.0000"), "USD")


# ── Checkout freezes the sale ────────────────────────────────────────────────


async def test_an_order_keeps_its_price_list_vat_and_cost(
    client: AsyncClient, db_session: AsyncSession, segments
):
    variant = await _variant(db_session, segments, cost="2500.5")
    buyer = await _buyer(db_session, segments[SegmentType.CAFE].id)

    order = await _order(client, db_session, buyer, variant)
    line = await _line(db_session, order)
    assert order.segment_code == SegmentType.CAFE
    assert line.vat_rate == Decimal("20.00")
    assert line.unit_cost == Decimal("2500.5000")

    # Costed again later: the order keeps what it was sold at.
    await db_session.execute(
        ProductVariant.__table__.update().where(ProductVariant.id == variant.id).values(cost_price=Decimal("9999"))
    )
    await db_session.commit()
    assert (await _line(db_session, order)).unit_cost == Decimal("2500.5000")


async def test_an_account_in_no_group_is_recorded_as_perakende(client: AsyncClient, db_session: AsyncSession, segments):
    variant = await _variant(db_session, segments, cost=None)
    order = await _order(client, db_session, await _buyer(db_session, None), variant)
    assert order.segment_code == SegmentType.RETAIL
    # No cost on the product: none on the line, and the order went through.
    assert (await _line(db_session, order)).unit_cost is None


async def test_a_usd_cost_uses_the_rates_in_memory(
    client: AsyncClient, db_session: AsyncSession, segments, monkeypatch
):
    rates = Rates(bulletin_date=date.today(), try_per_unit={"USD": Decimal("41.5000"), "EUR": Decimal("48.0000")})
    monkeypatch.setattr(exchange_rates, "_cache", (time.monotonic(), rates))
    variant = await _variant(db_session, segments, cost="10", currency="USD")
    buyer = await _buyer(db_session, segments[SegmentType.RETAIL].id)

    line = await _line(db_session, await _order(client, db_session, buyer, variant))
    assert line.unit_cost == Decimal("415.0000")


async def test_a_usd_cost_never_sends_checkout_to_tcmb(
    client: AsyncClient, db_session: AsyncSession, segments, monkeypatch
):
    """Prices in TRY, cost in USD, no rates in memory: the order is placed
    without asking TCMB, and the line's cost is unknown rather than late."""
    monkeypatch.setattr(exchange_rates, "_cache", None)

    async def no_network():
        raise AssertionError("checkout asked TCMB for a cost")

    monkeypatch.setattr(exchange_rates, "_fetch", no_network)
    variant = await _variant(db_session, segments, cost="10", currency="USD")
    buyer = await _buyer(db_session, segments[SegmentType.RETAIL].id)

    line = await _line(db_session, await _order(client, db_session, buyer, variant))
    assert line.unit_cost is None


# ── The backfill ─────────────────────────────────────────────────────────────


async def test_the_backfill_takes_each_products_latest_worksheet(db_session: AsyncSession, segments):
    variant = await _variant(db_session, segments, cost=None)
    product_id = variant.product_id
    now = datetime.now(timezone.utc)

    def applied(cost: str, vat_mode: str, currency: str, at: datetime, summary: bool = True) -> PricingRecord:
        return PricingRecord(
            product_code=variant.sku,
            product_name="Maliyet",
            sheet={"cost": cost, "vat_mode": vat_mode, "currency": currency, "retail": {"vat_rate": "20"}},
            applied_at=at,
            applied_summary={"product_id": str(product_id)} if summary else None,
        )

    db_session.add_all([
        applied("500", "EXCL", "TRY", now - timedelta(days=30)),
        # The latest, and one from before the summary named its product: found by code.
        applied("60", "INCL", "EUR", now - timedelta(days=1), summary=False),
        # Saved, never applied: not a price anyone charged.
        PricingRecord(product_code=variant.sku, product_name="Taslak", sheet={"cost": "1"}),
    ])
    await db_session.commit()

    first = await backfill(db_session)
    await db_session.commit()
    assert first["changed"] >= 1
    assert await _cost(db_session, variant.id) == (Decimal("50.0000"), "EUR")

    again = await backfill(db_session)
    assert again["changed"] == 0
