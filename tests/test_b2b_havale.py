""""Bayide / Toptanda / Kafede Havale / EFT İndirimi": a B2B account paying by
transfer gets the havale discount on the lines whose product has *its own
list's* switch on — and only on those.

The havale discount is otherwise a retail perk, taken off a retail cart as a
whole; a B2B account pays its tier price net. The three switches make it a
per-product affair for the bayi, toptan and kafe lists, each reading its own:
a switched-on line takes the standard havale rate (the tier the whole cart
reaches) on its own line total at that list's price, and every other line —
including one switched on for another list — is charged in full.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice
from app.models.user import CustomerSegment, SegmentType, User
from app.services.checkout_engine import CENTRAL_STOCK, HAVALE_SWITCH
from tests.conftest import CONSENTS, auth_header
from tests.test_b2b_checkout import CORPORATE, ISTANBUL_ADDRESS, ISTANBUL_REGISTERED

D = Decimal
B2B = [SegmentType.DEALER, SegmentType.WHOLESALE, SegmentType.CAFE]
# Every B2B list pays the same here, retail half again: a test that read the wrong list would show it.
B2B_PRICE = {"mine": D("1000.00"), "others": D("1500.00"), "plain": D("800.00"), "big": D("15000.00")}
RETAIL_MARKUP = D("1.50")


async def _product(db: AsyncSession, segments, price: Decimal, switches: set[str]) -> ProductVariant:
    suffix = uuid.uuid4().hex[:6]
    product = Product(
        base_code=f"BHV-{suffix}", name=f"B2B Havale {suffix}", tags=[], **{s: True for s in switches}
    )
    db.add(product)
    await db.flush()
    variant = ProductVariant(product_id=product.id, sku=f"BHV-{suffix}")
    db.add(variant)
    await db.flush()
    for code in SegmentType:
        incl = (price * RETAIL_MARKUP).quantize(D("0.01")) if code == SegmentType.RETAIL else price
        db.add(VariantPrice(
            variant_id=variant.id, segment_id=segments[code].id, currency="TRY",
            price_excl_tax=(incl / D("1.20")).quantize(D("0.01")), price_incl_tax=incl, vat_rate=D("20.00"),
        ))
    # One stock for every price list.
    db.add(Inventory(variant_id=variant.id, warehouse_type=CENTRAL_STOCK, quantity=100))
    await db.commit()
    return variant


async def _cart_for(db: AsyncSession, segments, segment: SegmentType) -> dict[str, ProductVariant]:
    """Its own switch on; the two other lists' switches on; none; and a big plain one."""
    mine = HAVALE_SWITCH[segment]
    others = set(HAVALE_SWITCH.values()) - {mine}
    return {
        "mine": await _product(db, segments, B2B_PRICE["mine"], {mine}),
        "others": await _product(db, segments, B2B_PRICE["others"], others),
        "plain": await _product(db, segments, B2B_PRICE["plain"], set()),
        "big": await _product(db, segments, B2B_PRICE["big"], set()),
    }


async def _firm(client: AsyncClient, db: AsyncSession, segments, segment: SegmentType) -> dict[str, str]:
    """A shop that signed up through the form, then put on a price list by the admin."""
    email = f"b2b-havale-{uuid.uuid4().hex[:8]}@test.com"
    resp = await client.post("/api/v1/auth/register", json={
        "account_type": "TOBACCO_SHOP", "email": email, "password": "Gizli-Parola-123",
        "contact_name": "Mehmet Demir", "phone": "0532 444 55 66",
        "company": CORPORATE, "billing_address": ISTANBUL_REGISTERED,
    })
    assert resp.status_code == 201, resp.text
    await db.execute(update(User).where(User.email == email).values(segment_id=segments[segment].id))
    await db.commit()
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": "Gizli-Parola-123"})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _body(lines: list[tuple[ProductVariant, int]], payment: str = "BANK_TRANSFER") -> dict:
    return {
        **CONSENTS,
        "payment_method": payment,
        "items": [{"variant_id": str(v.id), "quantity": q} for v, q in lines],
        "shipping_method": "YURTICI_DOOR",
        "shipping_consent": True,
        "shipping_address": ISTANBUL_ADDRESS,
    }


async def _quote(client: AsyncClient, headers: dict, lines, payment: str = "BANK_TRANSFER") -> dict:
    body = _body(lines, payment)
    body.pop("shipping_address")
    resp = await client.post("/api/v1/orders/calculate-cart", headers=headers, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _line(quote: dict, variant: ProductVariant) -> dict:
    return next(i for i in quote["items"] if i["variant_id"] == str(variant.id))


@pytest.mark.parametrize("segment", B2B, ids=lambda s: s.value)
async def test_each_list_gets_it_on_its_own_switched_on_lines_only(client: AsyncClient, db_session, segments, segment):
    cart = await _cart_for(db_session, segments, segment)
    firm = await _firm(client, db_session, segments, segment)
    quote = await _quote(client, firm, [(cart["mine"], 2), (cart["others"], 1), (cart["plain"], 1)])

    # List prices: 2 × 1,000 + 1,500 + 800.
    assert D(quote["subtotal"]) == D("4300.00")
    assert quote["perks_eligible"] is False  # still no retail perks
    mine, others, plain = (_line(quote, cart[k]) for k in ("mine", "others", "plain"))
    assert mine["havale_eligible"] is True and D(mine["havale_discount"]) == D("100.00")  # 5% of 2,000
    # Switched on for the two other lists: nothing for this one.
    assert others["havale_eligible"] is False and D(others["havale_discount"]) == D("0.00")
    assert plain["havale_eligible"] is False and D(plain["havale_discount"]) == D("0.00")
    assert D(quote["discount_rate"]) == D("0.05")
    assert D(quote["discount_amount"]) == D("100.00")  # not 5% of the whole 4,300
    assert D(quote["volume_discount_amount"]) == D("0.00")
    assert D(quote["net_total"]) == D("4200.00")

    # The invoice: lines before VAT, the discount before VAT, VAT on what is left.
    assert D(quote["subtotal_excl_tax"]) == D("3583.34")  # 1,666.67 + 1,250.00 + 666.67
    assert D(quote["line_discount_excl_tax"]) == D("83.34")  # 1,666.67 − 1,583.33
    assert D(quote["vat_total"]) == D("700.00")  # 4,200 − 3,500.00
    assert (
        D(quote["subtotal_excl_tax"]) - D(quote["line_discount_excl_tax"]) + D(quote["vat_total"])
        == D(quote["net_total"])
    )


@pytest.mark.parametrize("segment", B2B, ids=lambda s: s.value)
async def test_by_card_nothing_comes_off_but_the_line_says_it_would(client: AsyncClient, db_session, segments, segment):
    cart = await _cart_for(db_session, segments, segment)
    firm = await _firm(client, db_session, segments, segment)
    quote = await _quote(client, firm, [(cart["mine"], 2), (cart["others"], 1)], payment="CREDIT_CARD")
    assert D(quote["discount_amount"]) == D("0.00") and D(quote["discount_rate"]) == D("0.00")
    assert D(quote["net_total"]) == D("3500.00")
    assert _line(quote, cart["mine"])["havale_eligible"] is True
    assert D(_line(quote, cart["mine"])["havale_discount"]) == D("0.00")


@pytest.mark.parametrize("segment", B2B, ids=lambda s: s.value)
async def test_the_rate_is_the_tier_of_the_whole_cart(client: AsyncClient, db_session, segments, segment):
    cart = await _cart_for(db_session, segments, segment)
    firm = await _firm(client, db_session, segments, segment)
    # 2,000 switched on + 30,000 not: the cart reaches the 7% tier, taken off the 2,000 alone.
    quote = await _quote(client, firm, [(cart["mine"], 2), (cart["big"], 2)])
    assert D(quote["subtotal"]) == D("32000.00")
    assert D(quote["discount_rate"]) == D("0.07")
    assert D(_line(quote, cart["mine"])["havale_discount"]) == D("140.00")
    assert D(_line(quote, cart["big"])["havale_discount"]) == D("0.00")
    assert D(quote["discount_amount"]) == D("140.00")
    assert D(quote["net_total"]) == D("31860.00")


async def test_without_its_switched_on_line_a_list_pays_in_full(client: AsyncClient, db_session, segments):
    cart = await _cart_for(db_session, segments, SegmentType.CAFE)
    kafe = await _firm(client, db_session, segments, SegmentType.CAFE)
    quote = await _quote(client, kafe, [(cart["others"], 2), (cart["plain"], 1)])
    assert D(quote["discount_amount"]) == D("0.00") and D(quote["net_total"]) == D("3800.00")
    assert D(quote["line_discount_excl_tax"]) == D("0.00")


async def test_retail_keeps_its_cart_wide_discount(client: AsyncClient, db_session, segments, customer_user: User):
    cart = await _cart_for(db_session, segments, SegmentType.DEALER)
    quote = await _quote(client, auth_header(customer_user), [(cart["mine"], 2), (cart["others"], 1)])
    # Retail prices (×1.5): 3,000 + 2,250. The whole cart takes 5%, switches or not.
    assert D(quote["subtotal"]) == D("5250.00")
    assert D(quote["discount_amount"]) == D("262.50")
    assert all(not line["havale_eligible"] and D(line["havale_discount"]) == 0 for line in quote["items"])


@pytest.mark.parametrize("segment", B2B, ids=lambda s: s.value)
async def test_the_order_records_the_discount(client: AsyncClient, db_session, segments, segment):
    cart = await _cart_for(db_session, segments, segment)
    firm = await _firm(client, db_session, segments, segment)
    resp = await client.post(
        "/api/v1/orders/checkout", headers=firm, json=_body([(cart["mine"], 2), (cart["others"], 1)])
    )
    assert resp.status_code == 201, resp.text
    order = resp.json()
    assert D(order["total_amount"]) == D("3500.00")
    assert D(order["discount_amount"]) == D("100.00")
    assert order["payment_method"] == "BANK_TRANSFER"


async def test_the_sheet_saves_all_three_switches(client: AsyncClient, admin_user: User):
    from tests.test_product_sheet import _sheet

    body = _sheet()
    body["rules"] = {
        **body["rules"],
        "has_dealer_havale_discount": False,
        "has_toptan_havale_discount": True,
        "has_cafe_havale_discount": True,
    }
    created = await client.post("/api/v1/admin/products/sheet", headers=auth_header(admin_user), json=body)
    assert created.status_code == 201, created.text
    rules = created.json()["rules"]
    assert (rules["has_dealer_havale_discount"], rules["has_toptan_havale_discount"], rules["has_cafe_havale_discount"]) == (
        False, True, True,
    )


@pytest.mark.parametrize("segment", B2B, ids=lambda s: s.value)
async def test_the_product_page_says_whether_this_list_gets_it(
    client: AsyncClient, db_session, segments, segment, customer_user: User
):
    """`segment_havale`: what the product page's "%5 Havale & EFT İndirimi" block for a
    firm reads — true only where checkout will take it off for this list."""
    cart = await _cart_for(db_session, segments, segment)
    firm = await _firm(client, db_session, segments, segment)

    async def says(variant: ProductVariant, headers: dict | None = None) -> bool:
        resp = await client.get(f"/api/v1/products/{variant.product_id}", headers=headers or {})
        assert resp.status_code == 200, resp.text
        return resp.json()["segment_havale"]

    assert await says(cart["mine"], firm) is True
    assert await says(cart["others"], firm) is False  # another list's switch
    assert await says(cart["plain"], firm) is False
    # A guest and a retail account: their havale discount is the cart-wide one, never this.
    assert await says(cart["mine"]) is False
    assert await says(cart["mine"], auth_header(customer_user)) is False
    # The catalog's cards carry it too.
    listed = await client.get("/api/v1/products", params={"ids": [str(cart["mine"].product_id)]}, headers=firm)
    assert [p["segment_havale"] for p in listed.json()] == [True]
