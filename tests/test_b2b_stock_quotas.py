"""B2B stock: each list buys from its own quota, and from nothing else.

The product sheet holds a stock quota per list (Perakende / Bayi / Toptan /
Kafe). A list is held to its own: a B2B quota left at 0 (or never set) is sold
out for that list, and so is one that has run down to 0 — however much the
retail stock, or another list's quota, still holds. A signed-in buyer is told
how many they may order; a visitor gets the stock flag alone.

Covers:
- stock_source, the rule itself, for every combination that matters
- the storefront flag and quantity for each B2B list, and for a visitor
- the cart reporting each line's limit
- checkout taking stock from the list's own quota only, refusing more than it
  holds (and saying so in Turkish), and a quota sold down to 0 staying sold out
- Perakende held to the retail stock the same way
- a cancellation putting the stock back where the order took it from
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
from app.services.checkout_engine import stock_source, warehouse_for_segment
from tests.conftest import auth_header
from tests.test_b2b_havale import _body, _firm, _line, _quote

D = Decimal
B2B = [SegmentType.DEALER, SegmentType.WHOLESALE, SegmentType.CAFE]
RETAIL = WarehouseType.RETAIL_STOCK


def _rows(**quantities: int) -> list[SimpleNamespace]:
    return [SimpleNamespace(warehouse_type=WarehouseType(w), quantity=q) for w, q in quantities.items()]


@pytest.mark.parametrize(
    ("segment", "rows", "expected"),
    [
        # Each list reads its own quota.
        (SegmentType.WHOLESALE, _rows(B2B_STOCK=10, RETAIL_STOCK=50), (WarehouseType.B2B_STOCK, 10)),
        (SegmentType.DEALER, _rows(DEALER_STOCK=80, RETAIL_STOCK=50), (WarehouseType.DEALER_STOCK, 80)),
        (SegmentType.CAFE, _rows(CAFE_STOCK=3), (WarehouseType.CAFE_STOCK, 3)),
        # 0, or no row at all, is sold out for the list — the retail stock does not stand in.
        (SegmentType.WHOLESALE, _rows(B2B_STOCK=0, RETAIL_STOCK=50), (WarehouseType.B2B_STOCK, 0)),
        (SegmentType.CAFE, _rows(RETAIL_STOCK=50), (WarehouseType.CAFE_STOCK, 0)),
        (SegmentType.DEALER, _rows(), (WarehouseType.DEALER_STOCK, 0)),
        # Nor does another list's quota.
        (SegmentType.CAFE, _rows(DEALER_STOCK=30, B2B_STOCK=30, RETAIL_STOCK=30), (WarehouseType.CAFE_STOCK, 0)),
        # Perakende: the retail stock, and the B2B quotas are no use to it either.
        (SegmentType.RETAIL, _rows(RETAIL_STOCK=0, B2B_STOCK=10, CAFE_STOCK=10, DEALER_STOCK=10), (RETAIL, 0)),
        (SegmentType.RETAIL, _rows(RETAIL_STOCK=7), (RETAIL, 7)),
        (None, _rows(RETAIL_STOCK=7), (RETAIL, 7)),
    ],
)
def test_stock_source(segment, rows, expected):
    assert stock_source(rows, segment) == expected


async def _product(db: AsyncSession, segments, **stock: int) -> ProductVariant:
    """Priced for every list (1,000 TRY), with the given quotas."""
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"STK-{suffix}", name=f"Kota {suffix}", tags=[])
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
    for warehouse, quantity in stock.items():
        db.add(Inventory(variant_id=variant.id, warehouse_type=WarehouseType(warehouse), quantity=quantity))
    await db.commit()
    return variant


async def _quotas(db: AsyncSession, variant: ProductVariant) -> dict[WarehouseType, int]:
    # Fresh from the database: the API has changed them in this session since they were loaded.
    rows = await db.scalars(
        select(Inventory).where(Inventory.variant_id == variant.id).execution_options(populate_existing=True)
    )
    return {WarehouseType(r.warehouse_type): r.quantity for r in rows}


async def _storefront(client: AsyncClient, variant: ProductVariant, headers: dict | None = None) -> dict:
    page = await client.get(f"/api/v1/products/{variant.product_id}", headers=headers or {})
    assert page.status_code == 200, page.text
    return page.json()["variants"][0]


def _own(segment: SegmentType) -> str:
    return warehouse_for_segment(segment).value


@pytest.mark.parametrize("segment", B2B, ids=lambda s: s.value)
async def test_the_storefront_shows_the_lists_own_quota(client, db_session, segments, segment):
    firm = await _firm(client, db_session, segments, segment)
    stocked = await _product(db_session, segments, **{_own(segment): 10, "RETAIL_STOCK": 50})
    unset = await _product(db_session, segments, **{_own(segment): 0, "RETAIL_STOCK": 50})
    no_row = await _product(db_session, segments, RETAIL_STOCK=50)

    for variant, in_stock, available in ((stocked, True, 10), (unset, False, 0), (no_row, False, 0)):
        seen = await _storefront(client, variant, firm)
        assert (seen["in_stock"], seen["available_quantity"]) == (in_stock, available)

    # A visitor buys from the retail stock and gets the flag only, never a count.
    seen = await _storefront(client, unset)
    assert (seen["in_stock"], seen["available_quantity"]) == (True, None)


@pytest.mark.parametrize("segment", B2B, ids=lambda s: s.value)
async def test_a_list_buys_from_its_own_quota_until_it_runs_out(client, db_session, segments, segment):
    firm = await _firm(client, db_session, segments, segment)
    variant = await _product(db_session, segments, **{_own(segment): 10, "RETAIL_STOCK": 50})

    assert _line(await _quote(client, firm, [(variant, 4)]), variant)["available_quantity"] == 10
    # Not topped up from the retail stock: 11 is one more than the quota holds.
    resp = await client.post("/api/v1/orders/checkout", headers=firm, json=_body([(variant, 11)]))
    assert resp.status_code == 422
    assert "stokta yalnızca 10 adet var" in resp.json()["detail"]

    resp = await client.post("/api/v1/orders/checkout", headers=firm, json=_body([(variant, 10)]))
    assert resp.status_code == 201, resp.text
    quotas = await _quotas(db_session, variant)
    assert (quotas[warehouse_for_segment(segment)], quotas[RETAIL]) == (0, 50)  # the retail stock untouched
    item = await db_session.scalar(select(OrderItem).where(OrderItem.variant_id == variant.id).execution_options(populate_existing=True))
    assert item.stock_warehouse is warehouse_for_segment(segment)

    # Run down to 0, the quota is sold out for the list — fifty in retail or not.
    seen = await _storefront(client, variant, firm)
    assert (seen["in_stock"], seen["available_quantity"]) == (False, 0)
    assert _line(await _quote(client, firm, [(variant, 1)]), variant)["available_quantity"] == 0
    resp = await client.post("/api/v1/orders/checkout", headers=firm, json=_body([(variant, 1)]))
    assert resp.status_code == 422
    assert "tükendi" in resp.json()["detail"]


@pytest.mark.parametrize("segment", B2B, ids=lambda s: s.value)
async def test_a_quota_left_at_zero_is_sold_out_for_the_list(client, db_session, segments, segment):
    firm = await _firm(client, db_session, segments, segment)
    variant = await _product(db_session, segments, **{_own(segment): 0, "RETAIL_STOCK": 50})
    resp = await client.post("/api/v1/orders/checkout", headers=firm, json=_body([(variant, 1)]))
    assert resp.status_code == 422
    assert "tükendi" in resp.json()["detail"]
    assert (await _quotas(db_session, variant))[RETAIL] == 50


async def test_perakende_is_held_to_the_retail_stock(client, db_session, segments):
    shopper = await _firm(client, db_session, segments, SegmentType.RETAIL)
    variant = await _product(
        db_session, segments, RETAIL_STOCK=0, B2B_STOCK=10, CAFE_STOCK=10, DEALER_STOCK=10
    )
    seen = await _storefront(client, variant, shopper)
    assert (seen["in_stock"], seen["available_quantity"]) == (False, 0)
    resp = await client.post("/api/v1/orders/checkout", headers=shopper, json=_body([(variant, 1)]))
    assert resp.status_code == 422
    assert await _quotas(db_session, variant) == {
        RETAIL: 0, WarehouseType.B2B_STOCK: 10, WarehouseType.CAFE_STOCK: 10, WarehouseType.DEALER_STOCK: 10,
    }


async def test_a_cancellation_puts_the_stock_back_where_it_came_from(
    client, db_session, segments, admin_user: User
):
    firm = await _firm(client, db_session, segments, SegmentType.WHOLESALE)
    variant = await _product(db_session, segments, B2B_STOCK=10, RETAIL_STOCK=50)
    before = await _quotas(db_session, variant)
    resp = await client.post("/api/v1/orders/checkout", headers=firm, json=_body([(variant, 4)]))
    assert resp.status_code == 201, resp.text
    assert (await _quotas(db_session, variant))[WarehouseType.B2B_STOCK] == 6

    cancel = await client.post(
        f"/api/v1/admin/orders/{resp.json()['id']}/cancel",
        headers=auth_header(admin_user), json={"reason": "Test iptali"},
    )
    assert cancel.status_code == 200, cancel.text
    assert await _quotas(db_session, variant) == before
