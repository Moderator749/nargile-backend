"""The admin products table: sorting, (soft) delete and deep duplicate."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice, WarehouseType
from app.models.promotion import FlashSale
from app.models.user import User
from tests.conftest import ADDRESS, CONSENTS, auth_header


def _sheet(code: str, name: str, *, price: str, stock: int, **overrides) -> dict:
    return {
        "base_code": code,
        "name": name,
        "stock": {"quantity": stock},
        "prices": [{"currency": "TRY", "cells": {"RETAIL": {"price_excl_tax": price, "vat_rate": "20.00"}}}],
        "rules": {},
        "opportunity": {},
        **overrides,
    }


async def _save(client: AsyncClient, admin: User, body: dict) -> dict:
    resp = await client.post("/api/v1/admin/products/sheet", headers=auth_header(admin), json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def grid(client: AsyncClient, admin_user: User):
    """Three products whose code, name, stock and price all order differently."""
    suffix = uuid.uuid4().hex[:5].upper()
    a = await _save(client, admin_user, _sheet(f"GC-{suffix}", f"Çaydanlık {suffix}", price="300.00", stock=5))
    b = await _save(client, admin_user, _sheet(f"GA-{suffix}", f"Bardak {suffix}", price="100.00", stock=9))
    c = await _save(client, admin_user, _sheet(f"GB-{suffix}", f"Ağızlık {suffix}", price="200.00", stock=1))
    return {"suffix": suffix, "a": a, "b": b, "c": c}


async def _codes(client: AsyncClient, admin: User, suffix: str, **params) -> list[str]:
    resp = await client.get("/api/v1/admin/products", headers=auth_header(admin), params={"q": suffix, **params})
    assert resp.status_code == 200, resp.text
    return [p["base_code"] for p in resp.json()]


# ── Sorting ──────────────────────────────────────────────────────────────────


async def test_default_order_is_by_name(client: AsyncClient, admin_user: User, grid):
    codes = await _codes(client, admin_user, grid["suffix"])
    assert codes == [grid["c"]["base_code"], grid["b"]["base_code"], grid["a"]["base_code"]]  # Ağızlık, Bardak, Çay…


@pytest.mark.parametrize(
    ("order_by", "ascending"),
    [
        # Codes GA/GB/GC, prices 100/200/300 and stocks 9/1/5 each give a different
        # order, so a wrong column cannot pass by accident.
        ("code", ("b", "c", "a")),
        ("price", ("b", "c", "a")),
        ("stock", ("c", "a", "b")),
        ("name", ("c", "b", "a")),
    ],
)
async def test_sorting_both_ways(client: AsyncClient, admin_user: User, grid, order_by: str, ascending: tuple[str, ...]):
    expected = [grid[key]["base_code"] for key in ascending]
    assert await _codes(client, admin_user, grid["suffix"], order_by=order_by, order_dir="asc") == expected
    assert await _codes(client, admin_user, grid["suffix"], order_by=order_by, order_dir="desc") == list(
        reversed(expected)
    )


async def test_a_product_without_a_price_sorts_last_either_way(client: AsyncClient, admin_user: User, grid):
    priceless = await _save(
        client,
        admin_user,
        {
            "base_code": f"GZ-{grid['suffix']}",
            "name": f"Fiyatsız {grid['suffix']}",
            "stock": {"quantity": 0},
            "prices": [{"currency": "USD", "cells": {"DEALER": {"price_incl_tax": "10.00"}}}],
            "rules": {},
            "opportunity": {},
        },
    )
    for direction in ("asc", "desc"):
        codes = await _codes(client, admin_user, grid["suffix"], order_by="price", order_dir=direction)
        assert codes[-1] == priceless["base_code"]


async def test_a_foreign_priced_product_sorts_by_its_price_in_lira(
    client: AsyncClient, admin_user: User, grid, monkeypatch
):
    """The grid's prices are 120 / 240 / 360 TRY incl. VAT; $7.50 at 40 TRY/USD
    is 300 TRY, so it sorts between the 240 and the 360 — not last, as if it
    had no price."""
    from app.api.v1 import admin_catalog
    from app.services.exchange_rates import Rates

    async def rates():
        return Rates(bulletin_date=datetime.now(timezone.utc).date(), try_per_unit={"USD": Decimal("40"), "EUR": Decimal("45")})

    monkeypatch.setattr(admin_catalog, "get_rates", rates)
    usd = await _save(
        client,
        admin_user,
        {
            "base_code": f"GU-{grid['suffix']}",
            "name": f"Dövizli {grid['suffix']}",
            "stock": {"quantity": 3},
            "prices": [{"currency": "USD", "cells": {"RETAIL": {"price_incl_tax": "7.50", "vat_rate": "20.00"}}}],
            "rules": {},
            "opportunity": {},
        },
    )
    order = [grid["b"]["base_code"], grid["c"]["base_code"], usd["base_code"], grid["a"]["base_code"]]
    assert await _codes(client, admin_user, grid["suffix"], order_by="price", order_dir="asc") == order
    assert await _codes(client, admin_user, grid["suffix"], order_by="price", order_dir="desc") == list(reversed(order))


async def test_without_rates_a_foreign_price_sorts_last(client: AsyncClient, admin_user: User, grid, monkeypatch):
    from app.api.v1 import admin_catalog
    from app.services.exchange_rates import RatesUnavailable

    async def no_rates():
        raise RatesUnavailable

    monkeypatch.setattr(admin_catalog, "get_rates", no_rates)
    usd = await _save(
        client,
        admin_user,
        {
            "base_code": f"GU-{grid['suffix']}",
            "name": f"Dövizli {grid['suffix']}",
            "stock": {"quantity": 3},
            "prices": [{"currency": "USD", "cells": {"RETAIL": {"price_incl_tax": "1.00", "vat_rate": "20.00"}}}],
            "rules": {},
            "opportunity": {},
        },
    )
    assert (await _codes(client, admin_user, grid["suffix"], order_by="price", order_dir="asc"))[-1] == usd["base_code"]


# ── Bulk delete ──────────────────────────────────────────────────────────────


async def test_bulk_delete_is_a_soft_delete(client: AsyncClient, admin_user: User, db_session: AsyncSession, grid):
    """The rows stay, with their colours, prices and stock; the product is marked
    deleted, unpublished and gone from the admin list unless asked for."""
    ids = [grid["a"]["id"], grid["b"]["id"]]
    variant_ids = [uuid.UUID(grid["a"]["variant_id"]), uuid.UUID(grid["b"]["variant_id"])]

    resp = await client.post("/api/v1/admin/products/delete", headers=auth_header(admin_user), json={"product_ids": ids})
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["deleted"]) == set(ids) and resp.json()["refused"] == {}

    rows = (
        await db_session.execute(
            select(Product.is_deleted, Product.deleted_at, Product.is_published)
            .where(Product.id.in_([uuid.UUID(i) for i in ids]))
            .execution_options(populate_existing=True)
        )
    ).all()
    assert len(rows) == 2
    assert all(deleted and at is not None and not published for deleted, at, published in rows)
    for table in (ProductVariant, VariantPrice, Inventory):
        column = ProductVariant.id if table is ProductVariant else table.variant_id
        assert (await db_session.execute(select(table.id).where(column.in_(variant_ids)))).all() != []

    assert await _codes(client, admin_user, grid["suffix"]) == [grid["c"]["base_code"]]
    listed = await client.get(
        "/api/v1/admin/products", headers=auth_header(admin_user), params={"q": grid["suffix"]}
    )
    assert listed.headers["X-Total-Count"] == "1"
    every = await _codes(client, admin_user, grid["suffix"], include_deleted="true")
    assert sorted(every) == sorted(grid[k]["base_code"] for k in "abc")


async def test_a_deleted_product_is_not_found_again(client: AsyncClient, admin_user: User, grid):
    """Not deleted twice, nor opened, published, restocked or copied."""
    gone = grid["a"]["id"]
    headers = auth_header(admin_user)
    await client.post("/api/v1/admin/products/delete", headers=headers, json={"product_ids": [gone]})

    again = await client.post("/api/v1/admin/products/delete", headers=headers, json={"product_ids": [gone]})
    assert again.json() == {"deleted": [], "refused": {gone: "Ürün bulunamadı"}}
    assert (await client.get(f"/api/v1/admin/products/{gone}/sheet", headers=headers)).status_code == 404
    published = await client.patch(f"/api/v1/admin/products/{gone}/publish", headers=headers, json={"is_published": True})
    assert published.status_code == 404
    stock = await client.patch(
        f"/api/v1/admin/products/{gone}/stock",
        headers=headers,
        json={"variants": [{"id": grid["a"]["variant_id"], "quantity": 3}]},
    )
    assert stock.status_code == 404
    copied = await client.post("/api/v1/admin/products/duplicate", headers=headers, json={"product_ids": [gone]})
    assert copied.status_code == 404


async def test_a_sold_product_is_deleted_and_its_order_kept(
    client: AsyncClient, admin_user: User, customer_user: User, db_session: AsyncSession, grid
):
    """No more refusing a product that is in an order: its order line keeps its
    colour and product, and the storefront and checkout no longer offer it."""
    sold = grid["a"]
    line = {"variant_id": sold["variant_id"], "quantity": 1}
    order = {**CONSENTS, "payment_method": "CREDIT_CARD", "shipping_address": ADDRESS, "shipping_consent": True}
    placed = await client.post(
        "/api/v1/orders/checkout", headers=auth_header(customer_user), json={**order, "items": [line]}
    )
    assert placed.status_code == 201, placed.text

    resp = await client.post(
        "/api/v1/admin/products/delete",
        headers=auth_header(admin_user),
        json={"product_ids": [sold["id"], grid["b"]["id"]]},
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["deleted"]) == {sold["id"], grid["b"]["id"]} and resp.json()["refused"] == {}

    # The order line still reaches its product.
    from app.models.order import OrderItem

    name = await db_session.scalar(
        select(Product.name)
        .join(ProductVariant, ProductVariant.product_id == Product.id)
        .join(OrderItem, OrderItem.variant_id == ProductVariant.id)
        .where(OrderItem.variant_id == uuid.UUID(sold["variant_id"]))
        .limit(1)
    )
    assert name == sold["name"]

    # Gone from the storefront: its page, the list, the search, the cart.
    product = await db_session.get(Product, uuid.UUID(sold["id"]))
    assert (await client.get(f"/api/v1/products/{product.slug}")).status_code == 404
    listed = await client.get("/api/v1/products", params={"q": grid["suffix"]})
    assert sold["id"] not in {p["id"] for p in listed.json()}
    lines = await client.get("/api/v1/products/cart-lines", params={"ids": [sold["variant_id"]]})
    assert lines.json() == []
    again = await client.post(
        "/api/v1/orders/checkout", headers=auth_header(customer_user), json={**order, "items": [line]}
    )
    assert again.status_code in (400, 409, 422) and "satışta değil" in again.text


# ── Deep duplicate ───────────────────────────────────────────────────────────


async def test_duplicate_copies_everything_as_a_draft(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, grid
):
    ends = datetime.now(timezone.utc) + timedelta(days=2)
    source = await _save(
        client,
        admin_user,
        _sheet(
            f"GD-{grid['suffix']}",
            f"Zengin Ürün {grid['suffix']}",
            price="400.00",
            stock=7,
            color_names=["Gümüş"],
            image_urls=["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"],
            youtube_video_url="https://youtu.be/dQw4w9WgXcQ",
            rules={"is_free_shipping": True, "free_shipping_min_quantity": 2, "has_volume_discount": False},
            opportunity={"is_opportunity": True, "opportunity_price": "350.00", "opportunity_end_date": ends.isoformat()},
            related_product_ids=[grid["b"]["id"]],
        ),
    )
    # Give the one colour a dealer quota too, so the copy has two to carry.
    colour = {**source["variants"][0], "stock": {"quantity": 10}}
    await client.put(
        f"/api/v1/admin/products/{source['id']}/sheet",
        headers=auth_header(admin_user),
        json={**source, "variants": [colour]},
    )

    resp = await client.post(
        "/api/v1/admin/products/duplicate", headers=auth_header(admin_user), json={"product_ids": [source["id"]]}
    )
    assert resp.status_code == 201, resp.text
    row = resp.json()[0]
    assert row["is_published"] is False  # a draft
    assert row["name"].endswith("(Kopya)")
    assert row["base_code"] != source["base_code"]

    copy = (await client.get(f"/api/v1/admin/products/{row['id']}/sheet", headers=auth_header(admin_user))).json()
    assert copy["stock"] == {"quantity": 10}
    assert copy["color_names"] == ["Gümüş"]
    assert copy["image_urls"] == ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"]
    assert copy["youtube_video_url"].endswith("dQw4w9WgXcQ")
    assert copy["rules"]["is_free_shipping"] is True
    assert copy["rules"]["free_shipping_min_quantity"] == 2
    assert copy["rules"]["has_volume_discount"] is False
    assert Decimal(copy["opportunity"]["opportunity_price"]) == Decimal("350.00")
    assert copy["related_product_ids"] == [grid["b"]["id"]]
    retail = next(r for r in copy["prices"] if r["currency"] == "TRY")["cells"]["RETAIL"]
    assert Decimal(retail["price_excl_tax"]) == Decimal("400.00")

    # The copy's own sale row exists, so its Fırsat block is live like the source's.
    sale = await db_session.scalar(
        select(FlashSale).where(FlashSale.variant_id == uuid.UUID(copy["variant_id"]))
    )
    assert sale is not None and sale.is_active

    # And its stock is its own, not shared with the source.
    rows = {
        i.warehouse_type: i.quantity
        for i in (
            await db_session.execute(
                select(Inventory).where(Inventory.variant_id == uuid.UUID(copy["variant_id"]))
            )
        ).scalars()
    }
    assert rows == {WarehouseType.RETAIL_STOCK: 10}


async def test_duplicating_a_product_from_before_the_sheet(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, grid
):
    """A product written before galleries and colour labels existed has NULL in
    those columns, not an empty array; the sheet still has to read and copy it."""
    source_id = uuid.UUID(grid["a"]["id"])
    await db_session.execute(
        update(Product).where(Product.id == source_id).values(color_names=None, image_urls=None)
    )
    await db_session.commit()

    resp = await client.post(
        "/api/v1/admin/products/duplicate", headers=auth_header(admin_user), json={"product_ids": [str(source_id)]}
    )
    assert resp.status_code == 201, resp.text
    copy = (
        await client.get(f"/api/v1/admin/products/{resp.json()[0]['id']}/sheet", headers=auth_header(admin_user))
    ).json()
    assert copy["color_names"] == [] and copy["image_urls"] == []


async def test_duplicating_twice_keeps_finding_a_free_code(client: AsyncClient, admin_user: User, grid):
    ids = {"product_ids": [grid["a"]["id"]]}
    first = (await client.post("/api/v1/admin/products/duplicate", headers=auth_header(admin_user), json=ids)).json()
    second = (await client.post("/api/v1/admin/products/duplicate", headers=auth_header(admin_user), json=ids)).json()
    assert first[0]["base_code"] != second[0]["base_code"]
    assert first[0]["variants"][0]["sku"] != second[0]["variants"][0]["sku"]


# ── Quick stock edit ─────────────────────────────────────────────────────────


def _central(row: dict) -> list[int]:
    return [sum(i["quantity"] for i in v["inventory"]) for v in row["variants"]]


async def test_the_stock_column_sets_a_colours_stock(client: AsyncClient, admin_user: User, grid):
    product = grid["a"]
    row = (await client.get("/api/v1/admin/products", headers=auth_header(admin_user), params={"q": product["base_code"]})).json()[0]
    variant_id = row["variants"][0]["id"]

    resp = await client.patch(
        f"/api/v1/admin/products/{row['id']}/stock",
        headers=auth_header(admin_user),
        json={"variants": [{"id": variant_id, "quantity": 42}]},
    )
    assert resp.status_code == 200, resp.text
    assert _central(resp.json()) == [42]
    again = (await client.get("/api/v1/admin/products", headers=auth_header(admin_user), params={"q": product["base_code"]})).json()[0]
    assert _central(again) == [42]


async def test_the_stock_edit_sets_each_colour(client: AsyncClient, admin_user: User):
    body = _sheet(f"SC-{uuid.uuid4().hex[:5].upper()}", "İki Renk", price="10.00", stock=0)
    body |= {"color_names": ["Siyah", "Beyaz"], "variants": [{"color": "Siyah", "stock": {"quantity": 5}}, {"color": "Beyaz", "stock": {"quantity": 0}}]}
    await _save(client, admin_user, body)
    row = (await client.get("/api/v1/admin/products", headers=auth_header(admin_user), params={"q": body["base_code"]})).json()[0]
    siyah, beyaz = row["variants"]

    resp = await client.patch(
        f"/api/v1/admin/products/{row['id']}/stock",
        headers=auth_header(admin_user),
        json={"variants": [{"id": beyaz["id"], "quantity": 7}]},
    )
    assert _central(resp.json()) == [5, 7]  # the colour not sent keeps its stock


@pytest.mark.parametrize("quantity", [-1, 1_000_001])
async def test_an_impossible_stock_is_refused(client: AsyncClient, admin_user: User, grid, quantity: int):
    row = (await client.get("/api/v1/admin/products", headers=auth_header(admin_user), params={"q": grid["a"]["base_code"]})).json()[0]
    resp = await client.patch(
        f"/api/v1/admin/products/{row['id']}/stock",
        headers=auth_header(admin_user),
        json={"variants": [{"id": row["variants"][0]["id"], "quantity": quantity}]},
    )
    assert resp.status_code == 422


async def test_another_products_colour_is_refused(client: AsyncClient, admin_user: User, customer_user: User, grid):
    h = auth_header(admin_user)
    a = (await client.get("/api/v1/admin/products", headers=h, params={"q": grid["a"]["base_code"]})).json()[0]
    b = (await client.get("/api/v1/admin/products", headers=h, params={"q": grid["b"]["base_code"]})).json()[0]
    url = f"/api/v1/admin/products/{a['id']}/stock"
    body = {"variants": [{"id": b["variants"][0]["id"], "quantity": 3}]}
    assert (await client.patch(url, headers=h, json=body)).status_code == 422
    assert (await client.patch(url, headers=auth_header(customer_user), json=body)).status_code == 403
