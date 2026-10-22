"""Flash sales ("Fırsat Ürünleri"): checkout pricing, catalog exposure, admin API."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, Product
from app.models.promotion import FlashSale
from app.models.user import User
from app.services.flash_sales import discounted_price
from tests.conftest import ADDRESS, CONSENTS, auth_header


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
async def sales(db_session: AsyncSession, catalog):
    """Factory for sales on the fixture variants; removes them afterwards so the
    real storefront's "Fırsat Ürünleri" never shows test products."""
    normal_v, heavy_v = catalog
    variant_ids = [normal_v.id, heavy_v.id]  # read now: attributes expire on rollback

    product_ids = [normal_v.product_id, heavy_v.product_id]

    async def make(
        variant=normal_v, price="3990.00", start=timedelta(hours=-1), end=timedelta(hours=1), active=True, stock=None
    ):
        """A sale as the product sheet writes it: the row plus the product's
        Fırsat block, which is what decides whether the offer is claimable."""
        sale = FlashSale(
            variant_id=variant.id,
            sale_price_incl_tax=Decimal(price),
            starts_at=_now() + start,
            ends_at=None if end is None else _now() + end,
            is_active=active,
        )
        db_session.add(sale)
        product = await db_session.get(Product, variant.product_id)
        product.is_opportunity = True
        product.opportunity_price = Decimal(price)
        product.opportunity_end_date = sale.ends_at
        product.opportunity_stock = stock
        await db_session.commit()
        return sale

    yield make
    await db_session.rollback()
    await db_session.execute(delete(FlashSale).where(FlashSale.variant_id.in_(variant_ids)))
    await db_session.execute(
        update(Product).where(Product.id.in_(product_ids)).values(is_opportunity=False, opportunity_price=None)
    )
    await db_session.commit()


async def _unit_price(client: AsyncClient, user: User, variant) -> Decimal:
    resp = await client.post(
        "/api/v1/orders/calculate-cart",
        headers=auth_header(user),
        json={"payment_method": "CREDIT_CARD", "items": [{"variant_id": str(variant.id), "quantity": 1}]},
    )
    assert resp.status_code == 200, resp.text
    return Decimal(resp.json()["items"][0]["unit_price"])


# ── Checkout charges the sale price only while it is live ────────────────────


async def test_live_sale_price_is_charged(client: AsyncClient, customer_user: User, catalog, sales):
    normal_v, _ = catalog
    await sales()
    assert await _unit_price(client, customer_user, normal_v) == Decimal("3990.00")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start": timedelta(hours=-2), "end": timedelta(seconds=-1)},  # ended: price reverts
        {"start": timedelta(hours=1), "end": timedelta(hours=2)},  # not started yet
        {"active": False},  # switched off by admin
    ],
)
async def test_non_live_sale_uses_regular_price(
    client: AsyncClient, customer_user: User, catalog, sales, kwargs: dict
):
    normal_v, _ = catalog
    await sales(**kwargs)
    assert await _unit_price(client, customer_user, normal_v) == Decimal("5000.00")


async def test_sale_reverts_once_it_expires(
    client: AsyncClient, customer_user: User, db_session: AsyncSession, catalog, sales
):
    normal_v, _ = catalog
    sale = await sales()
    assert await _unit_price(client, customer_user, normal_v) == Decimal("3990.00")

    # Time passes: move the end into the past instead of sleeping.
    sale.ends_at = _now() - timedelta(milliseconds=1)
    sale.starts_at = sale.ends_at - timedelta(hours=1)
    await db_session.commit()
    assert await _unit_price(client, customer_user, normal_v) == Decimal("5000.00")


def test_sale_never_raises_a_price():
    sale = FlashSale(sale_price_incl_tax=Decimal("3990.00"))
    assert discounted_price(Decimal("5000.00"), sale) == Decimal("3990.00")
    assert discounted_price(Decimal("3500.00"), sale) is None  # dealer already cheaper
    assert discounted_price(Decimal("3990.00"), sale) is None
    assert discounted_price(Decimal("5000.00"), None) is None


# ── Catalog exposure ─────────────────────────────────────────────────────────


async def _catalog_variant(client: AsyncClient, variant) -> dict:
    resp = await client.get("/api/v1/products/", params={"q": variant.sku})
    assert resp.status_code == 200
    return next(v for p in resp.json() for v in p["variants"] if v["id"] == str(variant.id))


async def test_catalog_shows_live_sale_with_vat_split(client: AsyncClient, catalog, sales):
    normal_v, _ = catalog
    sale = await sales()
    v = await _catalog_variant(client, normal_v)
    assert v["price"]["price_incl_tax"] == "5000.00"  # regular price stays visible
    assert v["flash_sale"]["price"]["price_incl_tax"] == "3990.00"
    assert v["flash_sale"]["price"]["price_excl_tax"] == "3381.36"  # 3990 / 1.18
    assert datetime.fromisoformat(v["flash_sale"]["ends_at"]) == sale.ends_at


async def test_catalog_hides_ended_sale(client: AsyncClient, catalog, sales):
    normal_v, _ = catalog
    await sales(start=timedelta(hours=-2), end=timedelta(seconds=-1))
    assert (await _catalog_variant(client, normal_v))["flash_sale"] is None


async def test_in_stock_flag_follows_retail_inventory(
    client: AsyncClient, db_session: AsyncSession, catalog
):
    normal_v, _ = catalog
    assert (await _catalog_variant(client, normal_v))["in_stock"] is True

    inv = await db_session.scalar(select(Inventory).where(Inventory.variant_id == normal_v.id))
    inv.quantity = 0
    await db_session.commit()
    assert (await _catalog_variant(client, normal_v))["in_stock"] is False


async def test_flash_sales_endpoint_keeps_a_missed_offer_on_the_list(client: AsyncClient, catalog, sales):
    """A deal whose deadline has passed stays listed, without a claimable price
    ("Fırsat Kaçtı"); only switching the opportunity off removes it."""
    normal_v, heavy_v = catalog
    await sales(variant=normal_v)
    await sales(variant=heavy_v, price="900.00", start=timedelta(hours=-2), end=timedelta(seconds=-1))

    resp = await client.get("/api/v1/products/flash-sales", params={"limit": 50})
    assert resp.status_code == 200
    variants = {v["id"]: v for p in resp.json() for v in p["variants"]}
    assert variants[str(normal_v.id)]["flash_sale"] is not None  # claimable
    assert variants[str(heavy_v.id)]["flash_sale"] is None  # missed, still listed


async def test_offer_without_a_deadline_runs_until_its_stock_runs_out(
    client: AsyncClient, customer_user: User, db_session: AsyncSession, catalog, sales
):
    normal_v, _ = catalog
    await sales(end=None, stock=5)  # no deadline, five to be had
    assert await _unit_price(client, customer_user, normal_v) == Decimal("3990.00")
    v = await _catalog_variant(client, normal_v)
    assert v["flash_sale"]["ends_at"] is None  # nothing to count down to

    product = await db_session.get(Product, normal_v.product_id)
    product.opportunity_stock = 0
    await db_session.commit()
    # Out of deal stock: the regular price applies, but the product stays listed.
    assert await _unit_price(client, customer_user, normal_v) == Decimal("5000.00")
    listed = (await client.get("/api/v1/products/flash-sales", params={"limit": 50})).json()
    assert str(normal_v.id) in {v["id"] for p in listed for v in p["variants"]}


async def test_buying_on_offer_spends_the_offer_stock(
    client: AsyncClient, customer_user: User, db_session: AsyncSession, catalog, sales
):
    """"Fırsat stoğu" runs down as the deal sells, and the deal ends at zero."""
    normal_v, _ = catalog
    product_id = normal_v.product_id  # read now: attributes expire on commit elsewhere
    await sales(end=None, stock=2)
    body = {
        **CONSENTS,
        "payment_method": "CREDIT_CARD",
        "items": [{"variant_id": str(normal_v.id), "quantity": 2}],
        "shipping_address": ADDRESS,
        "shipping_consent": True,
    }
    placed = await client.post("/api/v1/orders/checkout", headers=auth_header(customer_user), json=body)
    assert placed.status_code == 201, placed.text
    assert Decimal(placed.json()["total_amount"]) == Decimal("7980.00")  # 2 × the offer price

    # populate_existing: the API's own session wrote the quota down, so re-read it.
    product = await db_session.get(Product, product_id, populate_existing=True)
    assert product.opportunity_stock == 0
    # Spent: the next buyer pays the regular price, the product stays listed.
    assert await _unit_price(client, customer_user, normal_v) == Decimal("5000.00")


async def test_switching_the_opportunity_off_removes_it_from_the_list(
    client: AsyncClient, db_session: AsyncSession, catalog, sales
):
    normal_v, _ = catalog
    await sales()
    product = await db_session.get(Product, normal_v.product_id)
    product.is_opportunity = False
    await db_session.commit()
    listed = (await client.get("/api/v1/products/flash-sales", params={"limit": 50})).json()
    assert str(normal_v.id) not in {v["id"] for p in listed for v in p["variants"]}


# ── Admin API ────────────────────────────────────────────────────────────────


def _body(sku: str, **overrides) -> dict:
    return {
        "sku": sku,
        "sale_price_incl_tax": "3990.00",
        "starts_at": (_now() - timedelta(minutes=1)).isoformat(),
        "ends_at": (_now() + timedelta(hours=6)).isoformat(),
        **overrides,
    }


async def test_admin_flash_sale_crud(client: AsyncClient, admin_user: User, catalog, sales):
    normal_v, _ = catalog
    h = auth_header(admin_user)

    created = await client.post("/api/v1/admin/flash-sales", headers=h, json=_body(normal_v.sku))
    assert created.status_code == 201, created.text
    sale = created.json()
    assert sale["is_live"] is True
    assert sale["regular_price_incl_tax"] == "5000.00"

    dup = await client.post("/api/v1/admin/flash-sales", headers=h, json=_body(normal_v.sku))
    assert dup.status_code == 409

    updated = await client.put(
        f"/api/v1/admin/flash-sales/{sale['id']}", headers=h, json=_body(normal_v.sku, is_active=False)
    )
    assert updated.status_code == 200
    assert updated.json()["is_live"] is False

    listed = await client.get("/api/v1/admin/flash-sales", headers=h)
    assert any(s["id"] == sale["id"] for s in listed.json())

    assert (await client.delete(f"/api/v1/admin/flash-sales/{sale['id']}", headers=h)).status_code == 204
    assert (await client.delete(f"/api/v1/admin/flash-sales/{sale['id']}", headers=h)).status_code == 404


async def test_admin_lists_and_edits_a_sale_without_a_deadline(
    client: AsyncClient, admin_user: User, catalog, sales
):
    """Rows with no end date (the deals backfill wrote them; the column allows
    it) once made the admin list fail as a whole. They list, and stay open."""
    normal_v, _ = catalog
    h = auth_header(admin_user)
    open_ended = await sales(end=None)
    listed = await client.get("/api/v1/admin/flash-sales", headers=h)
    assert listed.status_code == 200, listed.text
    row = next(s for s in listed.json() if s["id"] == open_ended.id)
    assert row["ends_at"] is None and row["is_live"] is True

    # Saved again from the panel with no end: still no deadline.
    kept = await client.put(f"/api/v1/admin/flash-sales/{open_ended.id}", headers=h, json=_body(normal_v.sku, ends_at=None))
    assert kept.status_code == 200, kept.text
    assert kept.json()["ends_at"] is None and kept.json()["is_live"] is True


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({"sale_price_incl_tax": "5000.00"}, 422),  # not below RETAIL: not a sale
        ({"sale_price_incl_tax": "0"}, 422),
        ({"ends_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}, 422),  # ends before start
        ({"starts_at": "2026-10-29T10:00:00", "ends_at": "2026-10-30T10:00:00"}, 422),  # naive times
        ({"sku": "NO-SUCH-SKU"}, 404),
    ],
)
async def test_admin_flash_sale_validation(
    client: AsyncClient, admin_user: User, catalog, sales, overrides: dict, status: int
):
    normal_v, _ = catalog
    resp = await client.post(
        "/api/v1/admin/flash-sales", headers=auth_header(admin_user), json={**_body(normal_v.sku), **overrides}
    )
    assert resp.status_code == status, resp.text


async def test_admin_flash_sales_require_admin(client: AsyncClient, customer_user: User):
    assert (await client.get("/api/v1/admin/flash-sales")).status_code == 401
    resp = await client.get("/api/v1/admin/flash-sales", headers=auth_header(customer_user))
    assert resp.status_code == 403
