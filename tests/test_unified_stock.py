"""One stock for every buyer ("Tek Ortak Stok Havuzu").

A visitor, a Perakende buyer and the Bayi / Toptan / Kafe lists all draw from
the same central stock (`checkout_engine.CENTRAL_STOCK`), first come, first
served; only the prices differ by list. The per-segment quota rows the shop
used before are left in the database but are no longer stock: nothing reads
them and nothing writes them. A firm's account is told how many there are; a
bireysel buyer and a visitor get the stock flag alone.

Covers:
- stock_on_hand, the rule itself
- the storefront flag and count, the same stock for every list and a visitor
- a central stock of 0 sold out for everyone, whatever a retired quota says
- checkout taking every list's order from the central stock, refusing more
  than it holds (in Turkish), under the lock
- Perakende and a B2B list spending the same stock until it runs out
- the stock check holding every buyer to the central stock
- a cancellation putting the stock back into the central stock
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice, WarehouseType
from app.models.order import OrderItem
from app.models.user import SegmentType, User
from app.services.checkout_engine import CENTRAL_STOCK, stock_on_hand
from tests.conftest import auth_header
from tests.test_b2b_havale import _body, _firm, _line, _quote

D = Decimal
B2B = [SegmentType.DEALER, SegmentType.WHOLESALE, SegmentType.CAFE]
RETIRED = {"B2B_STOCK": 50, "CAFE_STOCK": 50, "DEALER_STOCK": 50}


def _rows(**quantities: int) -> list[SimpleNamespace]:
    return [SimpleNamespace(warehouse_type=WarehouseType(w), quantity=q) for w, q in quantities.items()]


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (_rows(RETAIL_STOCK=10), 10),
        # The retired quotas are not stock, whatever they hold.
        (_rows(RETAIL_STOCK=10, B2B_STOCK=99, CAFE_STOCK=99, DEALER_STOCK=99), 10),
        (_rows(RETAIL_STOCK=0, B2B_STOCK=99), 0),
        (_rows(B2B_STOCK=99), 0),
        (_rows(), 0),
        # A row run below zero by hand is sold out, not a debt to sell against.
        (_rows(RETAIL_STOCK=-3), 0),
    ],
)
def test_stock_on_hand(rows, expected):
    assert CENTRAL_STOCK is WarehouseType.RETAIL_STOCK
    assert stock_on_hand(rows) == expected


async def _product(db: AsyncSession, segments, central: int | None, **retired: int) -> ProductVariant:
    """Priced for every list (1,000 TRY), with `central` in stock (no row for None)
    and whatever retired quota rows are given."""
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"STK-{suffix}", name=f"Stok {suffix}", tags=[])
    db.add(product)
    await db.flush()
    variant = ProductVariant(product_id=product.id, sku=f"STK-{suffix}")
    db.add(variant)
    await db.flush()
    for code in SegmentType:
        db.add(VariantPrice(
            variant_id=variant.id, segment_id=segments[code].id, currency="TRY",
            price_excl_tax=D("833.33"), price_incl_tax=D("1000.00"), vat_rate=D("20.00"),
        ))
    if central is not None:
        db.add(Inventory(variant_id=variant.id, warehouse_type=CENTRAL_STOCK, quantity=central))
    for warehouse, quantity in retired.items():
        db.add(Inventory(variant_id=variant.id, warehouse_type=WarehouseType(warehouse), quantity=quantity))
    await db.commit()
    return variant


async def _rows_of(db: AsyncSession, variant: ProductVariant) -> dict[WarehouseType, int]:
    # Fresh from the database: the API has changed them in this session since they were loaded.
    rows = await db.scalars(
        select(Inventory).where(Inventory.variant_id == variant.id).execution_options(populate_existing=True)
    )
    return {WarehouseType(r.warehouse_type): r.quantity for r in rows}


async def _central(db: AsyncSession, variant: ProductVariant) -> int:
    return (await _rows_of(db, variant))[CENTRAL_STOCK]


async def _storefront(client: AsyncClient, variant: ProductVariant, headers: dict | None = None) -> dict:
    page = await client.get(f"/api/v1/products/{variant.product_id}", headers=headers or {})
    assert page.status_code == 200, page.text
    return page.json()["variants"][0]


@pytest.mark.parametrize("segment", B2B, ids=lambda s: s.value)
async def test_every_list_reads_the_central_stock(client, db_session, segments, segment):
    firm = await _firm(client, db_session, segments, segment)
    shopper = await _firm(client, db_session, segments, SegmentType.RETAIL)
    # Its own retired quota at 0, the others at 50: neither counts.
    variant = await _product(db_session, segments, 10, **{**RETIRED, "B2B_STOCK": 0})

    # A firm is told the count — the shop's own, not a quota.
    seen = await _storefront(client, variant, firm)
    assert (seen["in_stock"], seen["available_quantity"]) == (True, 10)
    assert _line(await _quote(client, firm, [(variant, 4)]), variant)["available_quantity"] == 10
    # A bireysel buyer and a visitor read the same stock, without the number.
    for headers in (shopper, None):
        seen = await _storefront(client, variant, headers)
        assert (seen["in_stock"], seen["available_quantity"]) == (True, None)


@pytest.mark.parametrize("segment", [*B2B, SegmentType.RETAIL], ids=lambda s: s.value)
async def test_a_central_stock_of_zero_is_sold_out_for_everyone(client, db_session, segments, segment):
    buyer = await _firm(client, db_session, segments, segment)
    for variant in (
        await _product(db_session, segments, 0, **RETIRED),
        await _product(db_session, segments, None, **RETIRED),  # no central row at all
    ):
        assert (await _storefront(client, variant, buyer))["in_stock"] is False
        assert (await _storefront(client, variant))["in_stock"] is False
        resp = await client.post("/api/v1/orders/checkout", headers=buyer, json=_body([(variant, 1)]))
        assert resp.status_code == 422
        assert "tükendi" in resp.json()["detail"]
        # Nothing was taken from a retired quota either.
        assert {w: q for w, q in (await _rows_of(db_session, variant)).items() if w is not CENTRAL_STOCK} == {
            WarehouseType(w): q for w, q in RETIRED.items()
        }


@pytest.mark.parametrize("segment", B2B, ids=lambda s: s.value)
async def test_a_b2b_order_comes_out_of_the_central_stock(client, db_session, segments, segment):
    firm = await _firm(client, db_session, segments, segment)
    variant = await _product(db_session, segments, 10, **RETIRED)

    # 11 is one more than the shop has: refused, with the count a firm plans by.
    resp = await client.post("/api/v1/orders/checkout", headers=firm, json=_body([(variant, 11)]))
    assert resp.status_code == 422
    assert "stokta yalnızca 10 adet var" in resp.json()["detail"]

    resp = await client.post("/api/v1/orders/checkout", headers=firm, json=_body([(variant, 10)]))
    assert resp.status_code == 201, resp.text
    rows = await _rows_of(db_session, variant)
    assert rows[CENTRAL_STOCK] == 0
    assert {w: q for w, q in rows.items() if w is not CENTRAL_STOCK} == {WarehouseType(w): q for w, q in RETIRED.items()}
    item = await db_session.scalar(
        select(OrderItem).where(OrderItem.variant_id == variant.id).execution_options(populate_existing=True)
    )
    assert item.stock_warehouse is CENTRAL_STOCK

    # Sold down to 0, it is sold out for every buyer — fifty in a retired quota or not.
    assert (await _storefront(client, variant, firm))["available_quantity"] == 0
    assert (await _storefront(client, variant))["in_stock"] is False


async def test_retail_and_wholesale_spend_the_same_stock_until_it_runs_out(client, db_session, segments):
    firm = await _firm(client, db_session, segments, SegmentType.WHOLESALE)
    shopper = await _firm(client, db_session, segments, SegmentType.RETAIL)
    variant = await _product(db_session, segments, 5)

    assert (await client.post("/api/v1/orders/checkout", headers=firm, json=_body([(variant, 3)]))).status_code == 201
    assert await _central(db_session, variant) == 2

    # The bireysel buyer sees what the firm left, and is held to it without the count.
    assert (await _storefront(client, variant, shopper))["in_stock"] is True
    resp = await client.post("/api/v1/orders/checkout", headers=shopper, json=_body([(variant, 3)]))
    assert resp.status_code == 422
    assert "stok sınırına ulaştınız" in resp.json()["detail"]
    assert (await client.post("/api/v1/orders/checkout", headers=shopper, json=_body([(variant, 2)]))).status_code == 201
    assert await _central(db_session, variant) == 0

    # Now the firm finds it sold out, as does a visitor.
    resp = await client.post("/api/v1/orders/checkout", headers=firm, json=_body([(variant, 1)]))
    assert resp.status_code == 422 and "tükendi" in resp.json()["detail"]
    assert (await _storefront(client, variant))["in_stock"] is False


@pytest.mark.parametrize("segment", [*B2B, SegmentType.RETAIL, None], ids=lambda s: s.value if s else "visitor")
async def test_the_stock_check_holds_every_buyer_to_the_central_stock(client, db_session, segments, segment):
    headers = await _firm(client, db_session, segments, segment) if segment else {}
    variant = await _product(db_session, segments, 4, **RETIRED)
    resp = await client.post(
        "/api/v1/products/stock-check", headers=headers, json={"items": [{"variant_id": str(variant.id), "quantity": 9}]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == [{"variant_id": str(variant.id), "quantity": 4, "capped": True}]


async def test_a_cancellation_puts_the_stock_back_into_the_central_stock(
    client, db_session, segments, admin_user: User
):
    firm = await _firm(client, db_session, segments, SegmentType.WHOLESALE)
    variant = await _product(db_session, segments, 10, **RETIRED)
    before = await _rows_of(db_session, variant)
    resp = await client.post("/api/v1/orders/checkout", headers=firm, json=_body([(variant, 4)]))
    assert resp.status_code == 201, resp.text
    assert await _central(db_session, variant) == 6

    cancel = await client.post(
        f"/api/v1/admin/orders/{resp.json()['id']}/cancel",
        headers=auth_header(admin_user), json={"reason": "Test iptali"},
    )
    assert cancel.status_code == 200, cancel.text
    assert await _rows_of(db_session, variant) == before
