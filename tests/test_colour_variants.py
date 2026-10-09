"""Colours as the variant axis: one product, one card, a stock quota per colour.

Covers:
- the sheet round-trips a stock block per colour, each with four segment quotas
- buying one colour spends only that colour's quota
- the storefront serves one product with one variant per colour, in order
- a colour the admin removes goes, unless it has been sold — then it is retired:
  off the storefront and unbuyable, but its order line still names it
- re-adding a retired colour revives its own row, with its history
- a colour renamed by id keeps the stock that was attached to it
- "Açıklama ve SEO" round-trips and reaches the storefront
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, ProductVariant, WarehouseType
from app.models.user import CustomerSegment, SegmentType, User, UserRole
from tests.conftest import ADDRESS, CONSENTS, auth_header

QUOTAS = ("bayi", "toptan", "kafe", "perakende")


def _stock(bayi: int = 0, toptan: int = 0, kafe: int = 0, perakende: int = 0) -> dict:
    return {"bayi": bayi, "toptan": toptan, "kafe": kafe, "perakende": perakende}


def _sheet(*, colors: list[str] | None = None, variants: list[dict] | None = None, **overrides) -> dict:
    """A complete sheet, as the admin form posts it."""
    body = {
        "base_code": f"RNK-{uuid.uuid4().hex[:6].upper()}",
        "name": "Renkli Nargile",
        "color_names": colors if colors is not None else [],
        "prices": [
            {
                "currency": "TRY",
                "cells": {"RETAIL": {"price_excl_tax": "1000.00", "price_incl_tax": "1200.00"}},
            }
        ],
    }
    if variants is not None:
        body["variants"] = variants
    return body | overrides


async def _create(client: AsyncClient, admin: User, body: dict) -> dict:
    resp = await client.post("/api/v1/admin/products/sheet", headers=auth_header(admin), json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _update(client: AsyncClient, admin: User, product_id: str, body: dict) -> dict:
    resp = await client.put(
        f"/api/v1/admin/products/{product_id}/sheet", headers=auth_header(admin), json=body
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _quotas(db: AsyncSession, variant_id: str) -> dict[WarehouseType, int]:
    rows = (
        await db.execute(select(Inventory).where(Inventory.variant_id == uuid.UUID(variant_id)))
    ).scalars()
    return {row.warehouse_type: row.quantity for row in rows}


@pytest.fixture
async def two_colours(client: AsyncClient, admin_user: User) -> dict:
    """"Siyah" with 5 retail in stock, "Beyaz" with none."""
    return await _create(
        client,
        admin_user,
        _sheet(
            colors=["Siyah", "Beyaz"],
            variants=[
                {"color": "Siyah", "stock": _stock(perakende=5, kafe=2)},
                {"color": "Beyaz", "stock": _stock()},
            ],
        ),
    )


# ── The sheet ────────────────────────────────────────────────────────────────


async def test_each_colour_keeps_its_own_four_quotas(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, two_colours: dict
):
    siyah, beyaz = two_colours["variants"]
    assert [v["color"] for v in two_colours["variants"]] == ["Siyah", "Beyaz"]
    assert siyah["stock"] == _stock(perakende=5, kafe=2)
    assert beyaz["stock"] == _stock()
    # "Toplam Stok" is every quota of every colour.
    assert two_colours["stock_total"] == 7

    assert await _quotas(db_session, siyah["id"]) == {
        WarehouseType.DEALER_STOCK: 0,
        WarehouseType.B2B_STOCK: 0,
        WarehouseType.CAFE_STOCK: 2,
        WarehouseType.RETAIL_STOCK: 5,
    }
    assert (await _quotas(db_session, beyaz["id"]))[WarehouseType.RETAIL_STOCK] == 0


async def test_a_colour_added_later_opens_empty_and_can_be_stocked(
    client: AsyncClient, admin_user: User, two_colours: dict
):
    with_third = await _update(
        client,
        admin_user,
        two_colours["id"],
        {**two_colours, "color_names": ["Siyah", "Beyaz", "Bakır"]},
    )
    assert [v["color"] for v in with_third["variants"]] == ["Siyah", "Beyaz", "Bakır"]
    # The colours that were already there keep their quotas; the new one opens empty.
    assert with_third["variants"][0]["stock"] == _stock(perakende=5, kafe=2)
    assert with_third["variants"][2]["stock"] == _stock()
    assert with_third["variants"][2]["sku"].endswith("-BAKIR")

    bakir = {**with_third["variants"][2], "stock": _stock(bayi=9)}
    stocked = await _update(
        client, admin_user, two_colours["id"], {**with_third, "variants": [*with_third["variants"][:2], bakir]}
    )
    assert stocked["variants"][2]["stock"] == _stock(bayi=9)
    assert stocked["stock_total"] == 16


async def test_a_renamed_colour_keeps_its_stock(client: AsyncClient, admin_user: User, two_colours: dict):
    """The form sends the variant's id, so correcting a label is not a new colour."""
    siyah = two_colours["variants"][0]
    renamed = await _update(
        client,
        admin_user,
        two_colours["id"],
        {
            **two_colours,
            "color_names": ["Antrasit", "Beyaz"],
            "variants": [{**siyah, "color": "Antrasit"}, two_colours["variants"][1]],
        },
    )
    assert renamed["variants"][0]["id"] == siyah["id"]  # the same row, relabelled
    assert renamed["variants"][0]["color"] == "Antrasit"
    assert renamed["variants"][0]["stock"] == _stock(perakende=5, kafe=2)
    assert renamed["variants"][0]["sku"].endswith("-ANTRASIT")  # the code follows


async def test_two_labels_that_spell_the_same_code_still_get_one_each(
    client: AsyncClient, admin_user: User
):
    sheet = await _create(client, admin_user, _sheet(colors=["Mavi", "Mavi!"]))
    codes = [v["sku"] for v in sheet["variants"]]
    assert codes[0].endswith("-MAVI") and codes[1].endswith("-MAVI-2")
    assert len(set(codes)) == 2


async def test_a_colourless_product_still_has_one_unnamed_variant(
    client: AsyncClient, admin_user: User
):
    """Nothing about the colourless case changes: one variant, the product's code."""
    sheet = await _create(client, admin_user, _sheet(stock=_stock(perakende=4)))
    assert sheet["color_names"] == []
    assert [v["color"] for v in sheet["variants"]] == [None]
    assert sheet["variants"][0]["sku"] == sheet["base_code"] == sheet["sku"]
    assert sheet["variants"][0]["stock"] == _stock(perakende=4)

    product = (await client.get(f"/api/v1/products/{sheet['id']}")).json()
    assert [v["color_name"] for v in product["variants"]] == [None]


# ── Buying one colour ────────────────────────────────────────────────────────


async def test_buying_one_colour_leaves_the_others_alone(
    client: AsyncClient, admin_user: User, customer_user: User, db_session: AsyncSession
):
    sheet = await _create(
        client,
        admin_user,
        _sheet(
            colors=["Siyah", "Beyaz"],
            variants=[
                {"color": "Siyah", "stock": _stock(perakende=5)},
                {"color": "Beyaz", "stock": _stock(perakende=3)},
            ],
        ),
    )
    siyah, beyaz = sheet["variants"]

    resp = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(customer_user),
        json={**CONSENTS, 
            "payment_method": "CREDIT_CARD",
            "items": [{"variant_id": siyah["id"], "quantity": 2}],
            "shipping_address": ADDRESS,
            "shipping_consent": True,
        },
    )
    assert resp.status_code == 201, resp.text
    assert (await _quotas(db_session, siyah["id"]))[WarehouseType.RETAIL_STOCK] == 3
    assert (await _quotas(db_session, beyaz["id"]))[WarehouseType.RETAIL_STOCK] == 3


async def test_a_colour_can_be_sold_out_while_another_is_not(
    client: AsyncClient, admin_user: User, two_colours: dict
):
    """The card stays one product; the page's picker is what knows the difference."""
    listed = (await client.get("/api/v1/products/", params={"q": two_colours["base_code"]})).json()
    assert len(listed) == 1  # one item on the catalog, whatever its colours
    variants = listed[0]["variants"]
    assert [(v["color_name"], v["in_stock"]) for v in variants] == [("Siyah", True), ("Beyaz", False)]
    # Every colour is priced the same, from the sheet's one matrix.
    assert {v["price"]["price_incl_tax"] for v in variants} == {"1200.00"}
    assert listed[0]["color_names"] == ["Siyah", "Beyaz"]


async def test_each_colour_sells_from_its_own_segment_quota(
    client: AsyncClient,
    admin_user: User,
    db_session: AsyncSession,
    segments: dict[SegmentType, CustomerSegment],
):
    """A cafe buyer sees the cafe quota of the colour they picked, not the retail one."""
    sheet = await _create(
        client,
        admin_user,
        _sheet(
            colors=["Siyah", "Beyaz"],
            variants=[
                {"color": "Siyah", "stock": _stock(kafe=4)},
                {"color": "Beyaz", "stock": _stock(perakende=9)},
            ],
            prices=[
                {
                    "currency": "TRY",
                    "cells": {
                        "RETAIL": {"price_incl_tax": "1200.00"},
                        "CAFE": {"price_incl_tax": "1000.00"},
                    },
                }
            ],
        ),
    )
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

    page = (await client.get(f"/api/v1/products/{sheet['id']}", headers=auth_header(cafe))).json()
    # Beyaz has nine in the retail quota and none in the cafe one, so for a cafe
    # buyer it is the sold-out colour — the other way round from an anonymous visitor.
    assert [(v["color_name"], v["in_stock"]) for v in page["variants"]] == [("Siyah", True), ("Beyaz", False)]

    resp = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(cafe),
        json={**CONSENTS, 
            "payment_method": "CREDIT_CARD",
            "items": [{"variant_id": sheet["variants"][0]["id"], "quantity": 4}],
            "shipping_address": ADDRESS,
            "shipping_consent": True,
        },
    )
    assert resp.status_code == 201, resp.text
    quotas = await _quotas(db_session, sheet["variants"][0]["id"])
    assert quotas[WarehouseType.CAFE_STOCK] == 0 and quotas[WarehouseType.RETAIL_STOCK] == 0


async def test_the_cart_quote_names_the_colour(
    client: AsyncClient, admin_user: User, customer_user: User, two_colours: dict
):
    quote = await client.post(
        "/api/v1/orders/calculate-cart",
        headers=auth_header(customer_user),
        json={
            "payment_method": "CREDIT_CARD",
            "items": [{"variant_id": two_colours["variants"][0]["id"], "quantity": 1}],
        },
    )
    assert quote.status_code == 200, quote.text
    assert quote.json()["items"][0]["color_name"] == "Siyah"


# ── Retiring a colour ────────────────────────────────────────────────────────


async def test_removing_an_unsold_colour_deletes_it(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, two_colours: dict
):
    beyaz_id = uuid.UUID(two_colours["variants"][1]["id"])
    left = await _update(
        client,
        admin_user,
        two_colours["id"],
        {**two_colours, "color_names": ["Siyah"], "variants": [two_colours["variants"][0]]},
    )
    assert [v["color"] for v in left["variants"]] == ["Siyah"]
    assert await db_session.get(ProductVariant, beyaz_id) is None


async def test_removing_a_sold_colour_retires_it_instead(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
    db_session: AsyncSession,
    two_colours: dict,
):
    """Its order line has to keep naming what was sold, so the row stays — but
    nothing can reach it any more."""
    siyah = two_colours["variants"][0]
    order = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(customer_user),
        json={**CONSENTS, 
            "payment_method": "CREDIT_CARD",
            "items": [{"variant_id": siyah["id"], "quantity": 1}],
            "shipping_address": ADDRESS,
            "shipping_consent": True,
        },
    )
    assert order.status_code == 201, order.text

    left = await _update(
        client,
        admin_user,
        two_colours["id"],
        {**two_colours, "color_names": ["Beyaz"], "variants": [two_colours["variants"][1]]},
    )
    assert [v["color"] for v in left["variants"]] == ["Beyaz"]  # off the sheet

    # The app writes through this very session, so the row is already up to date.
    retired = await db_session.get(ProductVariant, uuid.UUID(siyah["id"]))
    assert retired is not None and retired.is_active is False
    # Every quota zeroed, so no path that forgets the flag can sell it either.
    assert set((await _quotas(db_session, siyah["id"])).values()) == {0}

    # Off the storefront, and refused if an old cart still holds it.
    page = (await client.get(f"/api/v1/products/{two_colours['id']}")).json()
    assert [v["color_name"] for v in page["variants"]] == ["Beyaz"]
    refused = await client.post(
        "/api/v1/orders/calculate-cart",
        headers=auth_header(customer_user),
        json={"payment_method": "CREDIT_CARD", "items": [{"variant_id": siyah["id"], "quantity": 1}]},
    )
    assert refused.status_code == 422 and "satışta değil" in refused.json()["detail"]

    # The order sheet still prints the colour that was sold.
    orders = (await client.get("/api/v1/admin/orders", headers=auth_header(admin_user))).json()
    line = next(i for o in orders for i in o["items"] if i["variant_id"] == siyah["id"])
    assert line["color_name"] == "Siyah"


async def test_re_adding_a_retired_colour_revives_its_own_row(
    client: AsyncClient,
    admin_user: User,
    customer_user: User,
    db_session: AsyncSession,
    two_colours: dict,
):
    siyah = two_colours["variants"][0]
    await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(customer_user),
        json={**CONSENTS, 
            "payment_method": "CREDIT_CARD",
            "items": [{"variant_id": siyah["id"], "quantity": 1}],
            "shipping_address": ADDRESS,
            "shipping_consent": True,
        },
    )
    without = await _update(
        client,
        admin_user,
        two_colours["id"],
        {**two_colours, "color_names": ["Beyaz"], "variants": [two_colours["variants"][1]]},
    )
    back = await _update(
        client, admin_user, two_colours["id"], {**without, "color_names": ["Beyaz", "Siyah"]}
    )
    revived = next(v for v in back["variants"] if v["color"] == "Siyah")
    # The same row, not a second one — which the unique (product, colour) pair
    # would have refused anyway.
    assert revived["id"] == siyah["id"]
    assert revived["stock"] == _stock()  # it was zeroed when it was retired


# ── "Açıklama ve SEO" ────────────────────────────────────────────────────────


async def test_description_and_seo_round_trip_to_the_storefront(
    client: AsyncClient, admin_user: User
):
    sheet = await _create(
        client,
        admin_user,
        _sheet(
            description="  Elmas kesim, 58 cm gövde.\nPirinç aksam.  ",
            meta_title="Elmas Kesim Nargile | Nargile Co",
            meta_description="58 cm gövdeli elmas kesim nargile, pirinç aksamlı.",
        ),
    )
    assert sheet["description"] == "Elmas kesim, 58 cm gövde.\nPirinç aksam."
    assert sheet["meta_title"] == "Elmas Kesim Nargile | Nargile Co"

    product = (await client.get(f"/api/v1/products/{sheet['id']}")).json()
    assert product["description"].startswith("Elmas kesim")
    assert product["meta_title"] == "Elmas Kesim Nargile | Nargile Co"
    assert product["meta_description"] == "58 cm gövdeli elmas kesim nargile, pirinç aksamlı."

    cleared = await _update(client, admin_user, sheet["id"], {**sheet, "meta_title": "   "})
    assert cleared["meta_title"] is None  # blank falls back to the product name


async def test_meta_fields_are_capped_at_what_a_serp_shows(client: AsyncClient, admin_user: User):
    resp = await client.post(
        "/api/v1/admin/products/sheet",
        headers=auth_header(admin_user),
        json=_sheet(meta_description="x" * 161),
    )
    assert resp.status_code == 422


# ── Duplicating a coloured product ───────────────────────────────────────────


async def test_duplicate_copies_every_colour_with_its_own_stock(
    client: AsyncClient, admin_user: User, two_colours: dict
):
    resp = await client.post(
        "/api/v1/admin/products/duplicate",
        headers=auth_header(admin_user),
        json={"product_ids": [two_colours["id"]]},
    )
    assert resp.status_code == 201, resp.text
    copy = (
        await client.get(
            f"/api/v1/admin/products/{resp.json()[0]['id']}/sheet", headers=auth_header(admin_user)
        )
    ).json()

    assert [v["color"] for v in copy["variants"]] == ["Siyah", "Beyaz"]
    assert copy["variants"][0]["stock"] == _stock(perakende=5, kafe=2)
    assert copy["variants"][1]["stock"] == _stock()
    # Its colours are its own rows, with their own codes.
    assert {v["id"] for v in copy["variants"]}.isdisjoint({v["id"] for v in two_colours["variants"]})
    assert copy["variants"][0]["sku"] == f"{copy['base_code']}-SIYAH"
    retail = next(r for r in copy["prices"] if r["currency"] == "TRY")["cells"]["RETAIL"]
    assert Decimal(retail["price_incl_tax"]) == Decimal("1200.00")


# ── Turning colours on and off for a product that is holding stock ───────────


async def test_adding_a_first_colour_keeps_the_stock_the_product_had(
    client: AsyncClient, admin_user: User, db_session: AsyncSession
):
    """The lone variant becomes the first colour, as the migration did — the
    alternative is a product that silently loses everything in its warehouse."""
    plain = await _create(client, admin_user, _sheet(stock=_stock(perakende=40, bayi=6)))
    before = plain["variants"][0]["id"]

    coloured = await _update(
        client,
        admin_user,
        plain["id"],
        {**plain, "color_names": ["Siyah", "Beyaz"], "variants": []},
    )
    assert [v["color"] for v in coloured["variants"]] == ["Siyah", "Beyaz"]
    assert coloured["variants"][0]["id"] == before  # the same row, now a colour
    assert coloured["variants"][0]["stock"] == _stock(perakende=40, bayi=6)
    assert coloured["variants"][1]["stock"] == _stock()
    assert coloured["stock_total"] == 46
    assert await db_session.get(ProductVariant, uuid.UUID(before)) is not None


async def test_dropping_the_last_colour_keeps_the_stock_too(
    client: AsyncClient, admin_user: User
):
    one = await _create(
        client,
        admin_user,
        _sheet(colors=["Siyah"], variants=[{"color": "Siyah", "stock": _stock(perakende=11)}]),
    )
    plain = await _update(client, admin_user, one["id"], {**one, "color_names": [], "variants": []})
    assert plain["color_names"] == []
    assert [v["color"] for v in plain["variants"]] == [None]
    assert plain["variants"][0]["id"] == one["variants"][0]["id"]
    assert plain["variants"][0]["stock"] == _stock(perakende=11)
    assert plain["variants"][0]["sku"] == plain["base_code"]  # back to the plain code


async def test_relabelling_the_only_colour_without_its_id_keeps_the_stock(
    client: AsyncClient, admin_user: User
):
    """A client that sends only the pills (no ids) still must not lose stock."""
    one = await _create(
        client,
        admin_user,
        _sheet(colors=["Siyah"], variants=[{"color": "Siyah", "stock": _stock(kafe=7)}]),
    )
    renamed = await _update(
        client, admin_user, one["id"], {**one, "color_names": ["Antrasit"], "variants": []}
    )
    assert [v["color"] for v in renamed["variants"]] == ["Antrasit"]
    assert renamed["variants"][0]["id"] == one["variants"][0]["id"]
    assert renamed["variants"][0]["stock"] == _stock(kafe=7)


async def test_two_colours_can_swap_their_labels_in_one_save(
    client: AsyncClient, admin_user: User, two_colours: dict
):
    """The stock stays with the row, so swapping the labels swaps the stock — and
    the codes that follow them cannot collide on the way through."""
    siyah, beyaz = two_colours["variants"]
    swapped = await _update(
        client,
        admin_user,
        two_colours["id"],
        {
            **two_colours,
            "color_names": ["Beyaz", "Siyah"],
            "variants": [{**siyah, "color": "Beyaz"}, {**beyaz, "color": "Siyah"}],
        },
    )
    assert [v["color"] for v in swapped["variants"]] == ["Beyaz", "Siyah"]
    assert [v["id"] for v in swapped["variants"]] == [siyah["id"], beyaz["id"]]
    assert [v["sku"] for v in swapped["variants"]] == [
        f"{two_colours['base_code']}-BEYAZ",
        f"{two_colours['base_code']}-SIYAH",
    ]
    # The quotas belong to the row, which is now the other colour.
    assert swapped["variants"][0]["stock"] == _stock(perakende=5, kafe=2)
    assert swapped["variants"][1]["stock"] == _stock()


async def test_reordering_colours_keeps_each_ones_stock(
    client: AsyncClient, admin_user: User, two_colours: dict
):
    siyah, beyaz = two_colours["variants"]
    reordered = await _update(
        client,
        admin_user,
        two_colours["id"],
        {**two_colours, "color_names": ["Beyaz", "Siyah"], "variants": [beyaz, siyah]},
    )
    assert [v["color"] for v in reordered["variants"]] == ["Beyaz", "Siyah"]
    assert [v["stock"]["perakende"] for v in reordered["variants"]] == [0, 5]
    # The codes follow the labels, so they did not move.
    assert [v["sku"] for v in reordered["variants"]] == [beyaz["sku"], siyah["sku"]]
