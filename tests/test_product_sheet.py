"""The admin product sheet: one save across five tables, and foreign-currency prices.

Covers:
- a sheet create writes info, colour, four stock quotas, the price matrix, the
  rule flags and the Fırsat block, and reads back exactly as saved
- an update clears a price cell and keeps a running sale's start time
- an opportunity needs a price and a future deadline
- each segment buys from its own stock quota
- a price entered in USD is shown as both currencies and charged in TRY
"""

import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice, WarehouseType
from app.models.promotion import FlashSale
from app.models.user import CustomerSegment, SegmentType, User, UserRole
from app.services import exchange_rates
from app.services.exchange_rates import Rates
from tests.conftest import ADDRESS, auth_header

USD_PER_TRY = Decimal("40.0000")


def _sheet(**overrides) -> dict:
    """A complete sheet, as the admin form posts it."""
    code = f"SHEET-{uuid.uuid4().hex[:6].upper()}"
    body = {
        "base_code": code,
        "name": "Elmas Kesim Nargile",
        "brand": "Nargile Co",
        "color_names": ["Antrasit", "Bakır", " antrasit "],
        "stock": {"bayi": 4, "toptan": 3, "kafe": 2, "perakende": 1},
        "prices": [
            {
                "currency": "TRY",
                "cells": {
                    "DEALER": {"price_excl_tax": "800.00", "price_incl_tax": "960.00"},
                    "WHOLESALE": {"price_excl_tax": "900.00", "price_incl_tax": "1080.00"},
                    "CAFE": {"price_incl_tax": "1100.00"},
                    "RETAIL": {"price_excl_tax": "1000.00", "price_incl_tax": "1200.00"},
                },
            }
        ],
        "rules": {
            "is_free_shipping": True,
            "free_shipping_min_quantity": 3,
            "has_havale_discount": False,
            "has_volume_discount": False,
            "gives_points": True,
            "can_use_points": False,
            "is_cart_rule_applied": True,
        },
        "image_urls": ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"],
        "youtube_video_url": "https://youtu.be/dQw4w9WgXcQ",
    }
    return body | overrides


async def _post(client: AsyncClient, admin: User, body: dict):
    return await client.post("/api/v1/admin/products/sheet", headers=auth_header(admin), json=body)


@pytest.fixture
def tcmb_rates():
    """Pin the rate cache so nothing reaches out to TCMB during a test."""
    previous = exchange_rates._cache
    exchange_rates._cache = (
        time.monotonic(),
        Rates(bulletin_date=date(2026, 10, 7), try_per_unit={"USD": USD_PER_TRY, "EUR": Decimal("45.0000")}),
    )
    yield
    exchange_rates._cache = previous


# ── The sheet round-trips ────────────────────────────────────────────────────


async def test_sheet_save_writes_every_section(client: AsyncClient, admin_user: User, db_session: AsyncSession):
    ends = datetime.now(timezone.utc) + timedelta(days=3)
    body = _sheet(
        opportunity={
            "is_opportunity": True,
            "opportunity_price": "899.00",
            "opportunity_stock": 25,
            "opportunity_end_date": ends.isoformat(),
            "opportunity_color": "#FFD400",
            "opp_is_free_shipping": True,
            "opp_free_shipping_min_quantity": 5,
            "opp_has_havale_discount": False,
            "opp_has_volume_discount": False,
            "opp_gives_points": False,
            "opp_can_use_points": True,
        }
    )
    created = await _post(client, admin_user, body)
    assert created.status_code == 201, created.text
    sheet = created.json()

    # Trimmed, de-duplicated (case-insensitively), in the admin's order.
    assert sheet["color_names"] == ["Antrasit", "Bakır"]
    # The gallery keeps its order and the cover is mirrored for one-photo readers.
    assert sheet["image_urls"] == ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"]
    assert sheet["youtube_video_url"] == "https://youtu.be/dQw4w9WgXcQ"
    # One variant per colour, in the admin's order, each with a stock code of its
    # own derived from the product code.
    assert [v["color"] for v in sheet["variants"]] == ["Antrasit", "Bakır"]
    assert [v["sku"] for v in sheet["variants"]] == [
        f"{body['base_code']}-ANTRASIT",
        f"{body['base_code']}-BAKIR",
    ]
    # `sku` is the stem the codes come from, so the sheet can be sent straight back.
    assert sheet["sku"] == body["base_code"]
    # `stock` reads back as the total across the colours; this sheet sent no
    # per-colour block, so all of it opened on the first colour.
    assert sheet["stock"] == {"bayi": 4, "toptan": 3, "kafe": 2, "perakende": 1}
    assert sheet["stock_total"] == 10
    assert sheet["variants"][0]["stock"] == {"bayi": 4, "toptan": 3, "kafe": 2, "perakende": 1}
    assert sheet["variants"][1]["stock"] == {"bayi": 0, "toptan": 0, "kafe": 0, "perakende": 0}
    assert sheet["rules"]["is_cart_rule_applied"] is True
    assert sheet["rules"]["has_havale_discount"] is False
    assert sheet["rules"]["has_volume_discount"] is False
    assert sheet["rules"]["free_shipping_min_quantity"] == 3

    # The matrix comes back per currency, with the blank side of a pair filled in.
    try_row = next(r for r in sheet["prices"] if r["currency"] == "TRY")
    assert Decimal(try_row["cells"]["RETAIL"]["price_incl_tax"]) == Decimal("1200.00")
    assert Decimal(try_row["cells"]["CAFE"]["price_excl_tax"]) == Decimal("916.67")  # 1100 at 20%

    # One quota row per segment, with the sheet's numbers.
    variant_id = uuid.UUID(sheet["variant_id"])
    quotas = {
        i.warehouse_type: i.quantity
        for i in (await db_session.execute(select(Inventory).where(Inventory.variant_id == variant_id))).scalars()
    }
    assert quotas == {
        WarehouseType.DEALER_STOCK: 4,
        WarehouseType.B2B_STOCK: 3,
        WarehouseType.CAFE_STOCK: 2,
        WarehouseType.RETAIL_STOCK: 1,
    }

    # The Fırsat block drives a live flash sale.
    sale = await db_session.scalar(select(FlashSale).where(FlashSale.variant_id == variant_id))
    assert sale is not None and sale.is_active
    assert sale.sale_price_incl_tax == Decimal("899.00")
    assert sheet["opportunity"]["opportunity_stock"] == 25
    assert sheet["opportunity"]["opportunity_color"] == "#FFD400"
    # The Fırsat block keeps its own copy of the rules, independent of the general ones.
    assert sheet["opportunity"]["opp_has_volume_discount"] is False
    assert sheet["opportunity"]["opp_free_shipping_min_quantity"] == 5


async def test_sheet_update_clears_a_price_and_keeps_the_sale_running(
    client: AsyncClient, admin_user: User, db_session: AsyncSession
):
    ends = datetime.now(timezone.utc) + timedelta(days=2)
    opportunity = {
        "is_opportunity": True,
        "opportunity_price": "899.00",
        "opportunity_end_date": ends.isoformat(),
    }
    created = (await _post(client, admin_user, _sheet(opportunity=opportunity))).json()
    variant_id = uuid.UUID(created["variant_id"])
    started = (await db_session.scalar(select(FlashSale).where(FlashSale.variant_id == variant_id))).starts_at

    # Resave with the cafe cell emptied and a cheaper deal.
    body = _sheet(base_code=created["base_code"], opportunity=opportunity | {"opportunity_price": "849.00"})
    body["prices"][0]["cells"]["CAFE"] = {}
    updated = await client.put(
        f"/api/v1/admin/products/{created['id']}/sheet", headers=auth_header(admin_user), json=body
    )
    assert updated.status_code == 200, updated.text
    try_row = next(r for r in updated.json()["prices"] if r["currency"] == "TRY")
    assert "CAFE" not in try_row["cells"]

    rows = (await db_session.execute(select(VariantPrice).where(VariantPrice.variant_id == variant_id))).scalars()
    assert len(list(rows)) == 3  # the cafe price row is gone

    db_session.expire_all()
    sale = await db_session.scalar(select(FlashSale).where(FlashSale.variant_id == variant_id))
    assert sale.sale_price_incl_tax == Decimal("849.00")
    assert sale.starts_at == started  # the countdown did not restart


async def test_opportunity_needs_a_price(client: AsyncClient, admin_user: User):
    opportunity = {"is_opportunity": True, "opportunity_end_date": "2030-01-01T00:00:00Z"}
    assert (await _post(client, admin_user, _sheet(opportunity=opportunity))).status_code == 422


async def test_opportunity_may_run_without_a_deadline(
    client: AsyncClient, admin_user: User, db_session: AsyncSession
):
    """No "Fırsat süresi": the offer runs until its stock or the admin stops it."""
    sheet = (
        await _post(
            client,
            admin_user,
            _sheet(opportunity={"is_opportunity": True, "opportunity_price": "10.00", "opportunity_stock": 3}),
        )
    ).json()
    assert sheet["opportunity"]["opportunity_end_date"] is None
    sale = await db_session.scalar(
        select(FlashSale).where(FlashSale.variant_id == uuid.UUID(sheet["variant_id"]))
    )
    assert sale is not None and sale.is_active and sale.ends_at is None


async def test_opportunity_deadline_must_be_in_the_future(client: AsyncClient, admin_user: User):
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    resp = await _post(
        client,
        admin_user,
        _sheet(opportunity={"is_opportunity": True, "opportunity_price": "10.00", "opportunity_end_date": past.isoformat()}),
    )
    assert resp.status_code == 422
    assert "gelecekte" in resp.json()["detail"]


async def test_sheet_rejects_a_code_another_product_owns(client: AsyncClient, admin_user: User):
    first = (await _post(client, admin_user, _sheet())).json()
    resp = await _post(client, admin_user, _sheet(base_code=first["base_code"]))
    assert resp.status_code == 422 and "başka bir ürüne ait" in resp.json()["detail"]


# ── Each segment buys from its own quota ─────────────────────────────────────


async def test_cafe_buys_from_the_cafe_quota(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, segments: dict[SegmentType, CustomerSegment]
):
    sheet = (await _post(client, admin_user, _sheet(stock={"bayi": 0, "toptan": 0, "kafe": 2, "perakende": 0}))).json()
    cafe = User(
        email=f"cafe-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.CUSTOMER,
        segment_id=segments[SegmentType.CAFE].id,
        is_active=True,
        is_verified=True,
    )
    db_session.add(cafe)
    await db_session.commit()

    body = {
        "payment_method": "CREDIT_CARD",
        "items": [{"variant_id": sheet["variant_id"], "quantity": 2}],
        "shipping_address": ADDRESS,
        "shipping_consent": True,
    }
    assert (await client.post("/api/v1/orders/checkout", headers=auth_header(cafe), json=body)).status_code == 201
    # Its own quota is spent; the other segments' quotas are untouched.
    quotas = {
        i.warehouse_type: i.quantity
        for i in (
            await db_session.execute(
                select(Inventory).where(Inventory.variant_id == uuid.UUID(sheet["variant_id"]))
            )
        ).scalars()
    }
    assert quotas[WarehouseType.CAFE_STOCK] == 0
    assert quotas[WarehouseType.RETAIL_STOCK] == 0


async def test_storefront_serves_the_gallery_video_and_badge_colour(client: AsyncClient, admin_user: User):
    ends = datetime.now(timezone.utc) + timedelta(days=1)
    body = _sheet(
        image_urls=["https://cdn.example.com/cover.jpg", "https://cdn.example.com/second.jpg"],
        youtube_video_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        opportunity={
            "is_opportunity": True,
            "opportunity_price": "500.00",
            "opportunity_end_date": ends.isoformat(),
            "opportunity_color": "#B4122B",
        },
    )
    sheet = (await _post(client, admin_user, body)).json()

    product = (await client.get(f"/api/v1/products/{sheet['id']}")).json()
    assert product["image_urls"] == ["https://cdn.example.com/cover.jpg", "https://cdn.example.com/second.jpg"]
    assert product["image_url"] == "https://cdn.example.com/cover.jpg"  # the cover, for cards
    assert product["youtube_video_url"].endswith("dQw4w9WgXcQ")
    assert product["opportunity_color"] == "#B4122B"


@pytest.mark.parametrize("url", ["https://vimeo.com/1", "not-a-url", "javascript:alert(1)"])
async def test_sheet_rejects_a_non_youtube_video(client: AsyncClient, admin_user: User, url: str):
    assert (await _post(client, admin_user, _sheet(youtube_video_url=url))).status_code == 422


# ── Publishing ───────────────────────────────────────────────────────────────


async def test_unpublished_product_leaves_the_storefront(client: AsyncClient, admin_user: User):
    """The row toggle takes a product off the catalog, the page and the Fırsat list,
    while the admin list keeps showing it."""
    ends = datetime.now(timezone.utc) + timedelta(days=1)
    sheet = (
        await _post(
            client,
            admin_user,
            _sheet(opportunity={"is_opportunity": True, "opportunity_price": "900.00", "opportunity_end_date": ends.isoformat()}),
        )
    ).json()
    code, pid = sheet["base_code"], sheet["id"]
    h = auth_header(admin_user)

    assert (await client.get("/api/v1/products/", params={"q": code})).json() != []
    assert (await client.get(f"/api/v1/products/{pid}")).status_code == 200

    off = await client.patch(f"/api/v1/admin/products/{pid}/publish", headers=h, json={"is_published": False})
    assert off.status_code == 200 and off.json()["is_published"] is False

    assert (await client.get("/api/v1/products/", params={"q": code})).json() == []
    assert (await client.get(f"/api/v1/products/{pid}")).status_code == 404
    firsat = (await client.get("/api/v1/products/flash-sales", params={"limit": 50})).json()
    assert pid not in {p["id"] for p in firsat}
    # The admin still sees it, with its state.
    listed = (await client.get("/api/v1/admin/products", headers=h, params={"q": code})).json()
    assert listed[0]["is_published"] is False

    on = await client.patch(f"/api/v1/admin/products/{pid}/publish", headers=h, json={"is_published": True})
    assert on.json()["is_published"] is True
    assert (await client.get(f"/api/v1/products/{pid}")).status_code == 200


async def test_an_unpublished_product_is_not_sold_alongside_another(client: AsyncClient, admin_user: User):
    pinned = (await _post(client, admin_user, _sheet())).json()
    main = (await _post(client, admin_user, _sheet(related_product_ids=[pinned["id"]]))).json()
    assert len((await client.get(f"/api/v1/products/{main['id']}")).json()["cross_sells"]) == 1

    await client.patch(
        f"/api/v1/admin/products/{pinned['id']}/publish", headers=auth_header(admin_user), json={"is_published": False}
    )
    assert (await client.get(f"/api/v1/products/{main['id']}")).json()["cross_sells"] == []


async def test_a_save_without_a_weight_keeps_the_stored_one(client: AsyncClient, admin_user: User):
    """Nothing in the admin UI edits the weight any more, so a save that leaves the
    field out must not wipe what the catalogue importer put there."""
    sheet = (await _post(client, admin_user, _sheet(weight_kg="1.250"))).json()
    assert Decimal(sheet["weight_kg"]) == Decimal("1.250")
    assert sheet["is_published"] is True

    body = _sheet(base_code=sheet["base_code"])  # no weight_kg at all
    resaved = await client.put(
        f"/api/v1/admin/products/{sheet['id']}/sheet", headers=auth_header(admin_user), json=body
    )
    assert resaved.status_code == 200, resaved.text
    assert Decimal(resaved.json()["weight_kg"]) == Decimal("1.250")

    cleared = await client.put(
        f"/api/v1/admin/products/{sheet['id']}/sheet",
        headers=auth_header(admin_user),
        json=_sheet(base_code=sheet["base_code"], weight_kg=None),
    )
    assert cleared.json()["weight_kg"] is None  # sending null still clears it


# ── "Birlikte satılanlar" ────────────────────────────────────────────────────


async def test_cross_sells_round_trip_and_reach_the_product_page(client: AsyncClient, admin_user: User):
    """Pinned products come back as cards for the form, and priced for the page."""
    first = (await _post(client, admin_user, _sheet(name="Yanında İyi Gider 1"))).json()
    second = (await _post(client, admin_user, _sheet(name="Yanında İyi Gider 2"))).json()
    main = (
        await _post(client, admin_user, _sheet(name="Ana Ürün", related_product_ids=[first["id"], second["id"]]))
    ).json()

    assert main["related_product_ids"] == [first["id"], second["id"]]
    cards = main["related_products"]
    assert [c["name"] for c in cards] == ["Yanında İyi Gider 1", "Yanında İyi Gider 2"]
    assert Decimal(cards[0]["price_incl_tax"]) == Decimal("1200.00")  # the public price
    assert cards[0]["currency"] == "TRY"

    # The product page carries them, priced like the product itself.
    page = (await client.get(f"/api/v1/products/{main['id']}")).json()
    assert [p["id"] for p in page["cross_sells"]] == [first["id"], second["id"]]
    assert page["cross_sells"][0]["variants"][0]["price"]["price_incl_tax"] == "1200.00"


async def test_cross_sells_are_replaced_on_save_and_capped_at_four(client: AsyncClient, admin_user: User):
    pinned = [(await _post(client, admin_user, _sheet())).json()["id"] for _ in range(5)]
    main = (await _post(client, admin_user, _sheet(related_product_ids=pinned[:2]))).json()

    body = _sheet(base_code=main["base_code"], related_product_ids=[pinned[2]])
    updated = await client.put(
        f"/api/v1/admin/products/{main['id']}/sheet", headers=auth_header(admin_user), json=body
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["related_product_ids"] == [pinned[2]]  # replaced, not added to

    too_many = _sheet(base_code=main["base_code"], related_product_ids=pinned)
    assert (
        await client.put(f"/api/v1/admin/products/{main['id']}/sheet", headers=auth_header(admin_user), json=too_many)
    ).status_code == 422


async def test_a_product_cannot_be_sold_with_itself(client: AsyncClient, admin_user: User):
    main = (await _post(client, admin_user, _sheet())).json()
    resp = await client.put(
        f"/api/v1/admin/products/{main['id']}/sheet",
        headers=auth_header(admin_user),
        json=_sheet(base_code=main["base_code"], related_product_ids=[main["id"]]),
    )
    assert resp.status_code == 422 and "kendisiyle" in resp.json()["detail"]


async def test_deleting_a_pinned_product_removes_the_link(
    client: AsyncClient, admin_user: User, db_session: AsyncSession
):
    """The cascade keeps the page from pointing at a product that is gone."""
    pinned = (await _post(client, admin_user, _sheet())).json()
    main = (await _post(client, admin_user, _sheet(related_product_ids=[pinned["id"]]))).json()

    # Products have no delete endpoint, so clear every colour's own rows the way
    # one would have to; what is being proved is that the cross-sell link goes
    # with the product.
    pinned_id = uuid.UUID(pinned["id"])
    variant_ids = [uuid.UUID(v["id"]) for v in pinned["variants"]]
    for table in (FlashSale, VariantPrice, Inventory):
        await db_session.execute(delete(table).where(table.variant_id.in_(variant_ids)))
    await db_session.execute(delete(ProductVariant).where(ProductVariant.id.in_(variant_ids)))
    await db_session.execute(delete(Product).where(Product.id == pinned_id))
    await db_session.commit()

    sheet = (await client.get(f"/api/v1/admin/products/{main['id']}/sheet", headers=auth_header(admin_user))).json()
    assert sheet["related_product_ids"] == [] and sheet["related_products"] == []
    assert (await client.get(f"/api/v1/products/{main['id']}")).json()["cross_sells"] == []


# ── Foreign-currency prices ──────────────────────────────────────────────────


async def test_usd_price_is_shown_in_both_currencies_and_charged_in_try(
    client: AsyncClient, admin_user: User, customer_user: User, tcmb_rates
):
    """Retail priced in USD: the catalog shows both, the cart locks TRY."""
    body = _sheet(
        stock={"bayi": 0, "toptan": 0, "kafe": 0, "perakende": 5},
        prices=[{"currency": "USD", "cells": {"RETAIL": {"price_excl_tax": "100.00", "price_incl_tax": "120.00"}}}],
    )
    sheet = (await _post(client, admin_user, body)).json()

    listed = (await client.get(f"/api/v1/products/{sheet['id']}")).json()["variants"][0]
    assert listed["origin_price"]["currency"] == "USD"
    assert Decimal(listed["origin_price"]["price_incl_tax"]) == Decimal("120.00")
    assert listed["price"]["currency"] == "TRY"
    assert Decimal(listed["price"]["price_incl_tax"]) == Decimal("120.00") * USD_PER_TRY

    quote = await client.post(
        "/api/v1/orders/calculate-cart",
        headers=auth_header(customer_user),
        json={"payment_method": "CREDIT_CARD", "items": [{"variant_id": sheet["variant_id"], "quantity": 1}]},
    )
    assert Decimal(quote.json()["subtotal"]) == Decimal("120.00") * USD_PER_TRY
