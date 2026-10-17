"""B2B stock: a list's own quota when the admin sets one, else the retail pool.

The product sheet holds a stock quota per list (Perakende / Bayi / Toptan /
Kafe). For Bayi, Toptan and Kafe a quota of 0 (or none at all) does not mean
sold out: it means nothing was set aside, and the list shares the Perakende
pool. A quota above 0 is a dedicated limit the list is held to instead.

Covers:
- stock_source, the rule itself, for every combination that matters
- the storefront flag and quantity for each B2B list, and for a visitor
- the cart reporting each line's limit
- checkout taking stock from the pool or the dedicated quota, refusing more
  than either allows, and saying so in Turkish
- Perakende unchanged: its 0 is sold out, whatever the B2B quotas hold
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
        # A B2B list with a quota of its own is held to it, even below the pool.
        (SegmentType.WHOLESALE, _rows(B2B_STOCK=10, RETAIL_STOCK=50), (WarehouseType.B2B_STOCK, 10)),
        (SegmentType.DEALER, _rows(DEALER_STOCK=80, RETAIL_STOCK=50), (WarehouseType.DEALER_STOCK, 80)),
        # 0, or no row at all: the retail pool.
        (SegmentType.WHOLESALE, _rows(B2B_STOCK=0, RETAIL_STOCK=50), (RETAIL, 50)),
        (SegmentType.CAFE, _rows(RETAIL_STOCK=50), (RETAIL, 50)),
        # Sold out only when the pool is empty too.
        (SegmentType.CAFE, _rows(CAFE_STOCK=0, RETAIL_STOCK=0), (RETAIL, 0)),
        (SegmentType.DEALER, _rows(), (RETAIL, 0)),
        # Another list's quota is no use to this one.
        (SegmentType.CAFE, _rows(DEALER_STOCK=30, B2B_STOCK=30, RETAIL_STOCK=0), (RETAIL, 0)),
        # Perakende never borrows: its 0 is sold out.
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
    product = Product(base_code=f"STK-{suffix}", name=f"Havuz {suffix}", tags=[])
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
async def test_the_storefront_shows_the_pool_or_the_dedicated_quota(client, db_session, segments, segment):
    firm = await _firm(client, db_session, segments, segment)
    pool = await _product(db_session, segments, **{_own(segment): 0, "RETAIL_STOCK": 50})
    dedicated = await _product(db_session, segments, **{_own(segment): 10, "RETAIL_STOCK": 50})
    gone = await _product(db_session, segments, **{_own(segment): 0, "RETAIL_STOCK": 0})

    for variant, in_stock, available in ((pool, True, 50), (dedicated, True, 10), (gone, False, 0)):
        seen = await _storefront(client, variant, firm)
        assert (seen["in_stock"], seen["available_quantity"]) == (in_stock, available)

    # A visitor buys from the retail pool and gets the flag only, never a count.
    seen = await _storefront(client, gone)
    assert (seen["in_stock"], seen["available_quantity"]) == (False, None)
    seen = await _storefront(client, dedicated)
    assert (seen["in_stock"], seen["available_quantity"]) == (True, None)


@pytest.mark.parametrize("segment", B2B, ids=lambda s: s.value)
async def test_a_list_without_a_quota_buys_from_the_retail_pool(client, db_session, segments, segment):
    firm = await _firm(client, db_session, segments, segment)
    variant = await _product(db_session, segments, **{_own(segment): 0, "RETAIL_STOCK": 50})

    quote = await _quote(client, firm, [(variant, 30)])
    assert _line(quote, variant)["available_quantity"] == 50

    resp = await client.post("/api/v1/orders/checkout", headers=firm, json=_body([(variant, 30)]))
    assert resp.status_code == 201, resp.text
    # Out of the pool; the list's own quota stays at 0, so it keeps sharing it.
    quotas = await _quotas(db_session, variant)
    assert (quotas[RETAIL], quotas[warehouse_for_segment(segment)]) == (20, 0)
    item = await db_session.scalar(select(OrderItem).where(OrderItem.variant_id == variant.id).execution_options(populate_existing=True))
    assert item.stock_warehouse is RETAIL

    # The pool is shared: what is left in it is all the list may have now.
    assert _line(await _quote(client, firm, [(variant, 21)]), variant)["available_quantity"] == 20
    resp = await client.post("/api/v1/orders/checkout", headers=firm, json=_body([(variant, 21)]))
    assert resp.status_code == 422
    assert "stokta yalnızca 20 adet var" in resp.json()["detail"]


@pytest.mark.parametrize("segment", B2B, ids=lambda s: s.value)
async def test_a_dedicated_quota_is_the_limit(client, db_session, segments, segment):
    firm = await _firm(client, db_session, segments, segment)
    variant = await _product(db_session, segments, **{_own(segment): 10, "RETAIL_STOCK": 50})

    # Not topped up from the pool: 11 is one more than was set aside.
    resp = await client.post("/api/v1/orders/checkout", headers=firm, json=_body([(variant, 11)]))
    assert resp.status_code == 422
    assert "stokta yalnızca 10 adet var" in resp.json()["detail"]

    resp = await client.post("/api/v1/orders/checkout", headers=firm, json=_body([(variant, 10)]))
    assert resp.status_code == 201, resp.text
    quotas = await _quotas(db_session, variant)
    assert (quotas[warehouse_for_segment(segment)], quotas[RETAIL]) == (0, 50)
    item = await db_session.scalar(select(OrderItem).where(OrderItem.variant_id == variant.id).execution_options(populate_existing=True))
    assert item.stock_warehouse is warehouse_for_segment(segment)


@pytest.mark.parametrize("segment", B2B, ids=lambda s: s.value)
async def test_sold_out_only_when_the_pool_is_empty_too(client, db_session, segments, segment):
    firm = await _firm(client, db_session, segments, segment)
    variant = await _product(db_session, segments, **{_own(segment): 0, "RETAIL_STOCK": 0})
    resp = await client.post("/api/v1/orders/checkout", headers=firm, json=_body([(variant, 1)]))
    assert resp.status_code == 422
    assert "tükendi" in resp.json()["detail"]


async def test_perakende_zero_is_sold_out_whatever_the_b2b_quotas_hold(client, db_session, segments):
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


@pytest.mark.parametrize(
    ("own_quota", "taken_from"),
    [(0, RETAIL), (10, WarehouseType.B2B_STOCK)],
    ids=["from-the-pool", "from-the-quota"],
)
async def test_a_cancellation_puts_the_stock_back_where_it_came_from(
    client, db_session, segments, admin_user: User, own_quota, taken_from
):
    firm = await _firm(client, db_session, segments, SegmentType.WHOLESALE)
    variant = await _product(db_session, segments, B2B_STOCK=own_quota, RETAIL_STOCK=50)
    before = await _quotas(db_session, variant)
    resp = await client.post("/api/v1/orders/checkout", headers=firm, json=_body([(variant, 4)]))
    assert resp.status_code == 201, resp.text
    assert (await _quotas(db_session, variant))[taken_from] == before[taken_from] - 4

    cancel = await client.post(
        f"/api/v1/admin/orders/{resp.json()['id']}/cancel",
        headers=auth_header(admin_user), json={"reason": "Test iptali"},
    )
    assert cancel.status_code == 200, cancel.text
    assert await _quotas(db_session, variant) == before
