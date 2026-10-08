"""The admin products table: sorting, bulk delete and deep duplicate."""

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
from tests.conftest import ADDRESS, auth_header


def _sheet(code: str, name: str, *, price: str, stock: int, **overrides) -> dict:
    return {
        "base_code": code,
        "name": name,
        "stock": {"bayi": 0, "toptan": 0, "kafe": 0, "perakende": stock},
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
            "stock": {"bayi": 0, "toptan": 0, "kafe": 0, "perakende": 0},
            "prices": [{"currency": "USD", "cells": {"DEALER": {"price_incl_tax": "10.00"}}}],
            "rules": {},
            "opportunity": {},
        },
    )
    for direction in ("asc", "desc"):
        codes = await _codes(client, admin_user, grid["suffix"], order_by="price", order_dir=direction)
        assert codes[-1] == priceless["base_code"]


# ── Bulk delete ──────────────────────────────────────────────────────────────


async def test_bulk_delete_removes_products_and_their_rows(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, grid
):
    ids = [grid["a"]["id"], grid["b"]["id"]]
    variant_ids = [uuid.UUID(grid["a"]["variant_id"]), uuid.UUID(grid["b"]["variant_id"])]

    resp = await client.post("/api/v1/admin/products/delete", headers=auth_header(admin_user), json={"product_ids": ids})
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["deleted"]) == set(ids) and resp.json()["refused"] == {}

    assert (await db_session.execute(select(Product.id).where(Product.id.in_([uuid.UUID(i) for i in ids])))).all() == []
    for table in (ProductVariant, VariantPrice, Inventory):
        column = ProductVariant.id if table is ProductVariant else table.variant_id
        assert (await db_session.execute(select(table.id).where(column.in_(variant_ids)))).all() == []
    assert await _codes(client, admin_user, grid["suffix"]) == [grid["c"]["base_code"]]


async def test_a_sold_product_is_kept_and_reported(
    client: AsyncClient, admin_user: User, customer_user: User, grid
):
    """Deleting it would take order lines with it, so it is refused by name."""
    sold = grid["a"]
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(customer_user),
        json={
            "payment_method": "CREDIT_CARD",
            "items": [{"variant_id": sold["variant_id"], "quantity": 1}],
            "shipping_address": ADDRESS,
            "shipping_consent": True,
        },
    )
    assert placed.status_code == 201, placed.text

    resp = await client.post(
        "/api/v1/admin/products/delete",
        headers=auth_header(admin_user),
        json={"product_ids": [sold["id"], grid["b"]["id"]]},
    )
    assert resp.json()["deleted"] == [grid["b"]["id"]]
    assert "siparişlerde kullanılıyor" in resp.json()["refused"][sold["id"]]
    assert sold["base_code"] in await _codes(client, admin_user, grid["suffix"])


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
    colour = {**source["variants"][0], "stock": {**source["variants"][0]["stock"], "bayi": 3}}
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
    assert copy["stock"] == {"bayi": 3, "toptan": 0, "kafe": 0, "perakende": 7}
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
    quotas = {
        i.warehouse_type: i.quantity
        for i in (
            await db_session.execute(
                select(Inventory).where(Inventory.variant_id == uuid.UUID(copy["variant_id"]))
            )
        ).scalars()
    }
    assert quotas[WarehouseType.RETAIL_STOCK] == 7 and quotas[WarehouseType.DEALER_STOCK] == 3


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
