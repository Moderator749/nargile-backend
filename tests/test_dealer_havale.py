""""Bayide Havale / EFT İndirimi": a bayi paying by transfer gets the havale
discount on the lines whose product has the switch on — and only on those.

The havale discount is otherwise a retail perk, taken off a retail cart as a
whole; a B2B account pays its tier price net. For a bayi the switch makes it a
per-product affair: each switched-on line takes the standard havale rate (the
tier the whole cart reaches) on its own line total at the bayi price, and every
other line is charged in full.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice
from app.models.user import CustomerSegment, SegmentType, User
from app.services.checkout_engine import warehouse_for_segment
from tests.conftest import CONSENTS, auth_header
from tests.test_b2b_checkout import CORPORATE, ISTANBUL_ADDRESS, ISTANBUL_REGISTERED

# The bayi list is cheaper than retail, so a test that read the wrong list would show it.
DEALER_PRICE = {"on": Decimal("1000.00"), "off": Decimal("1500.00"), "big": Decimal("15000.00")}
RETAIL_MARKUP = Decimal("1.50")


async def _product(db: AsyncSession, segments: dict[SegmentType, CustomerSegment], dealer_price: Decimal, switch: bool) -> ProductVariant:
    suffix = uuid.uuid4().hex[:6]
    product = Product(
        base_code=f"BHV-{suffix}", name=f"Bayi Havale {suffix}", tags=[], has_dealer_havale_discount=switch
    )
    db.add(product)
    await db.flush()
    variant = ProductVariant(product_id=product.id, sku=f"BHV-{suffix}")
    db.add(variant)
    await db.flush()
    for code in SegmentType:
        incl = dealer_price if code == SegmentType.DEALER else (dealer_price * RETAIL_MARKUP).quantize(Decimal("0.01"))
        db.add(VariantPrice(
            variant_id=variant.id, segment_id=segments[code].id, currency="TRY",
            price_excl_tax=(incl / Decimal("1.20")).quantize(Decimal("0.01")),
            price_incl_tax=incl, vat_rate=Decimal("20.00"),
        ))
        db.add(Inventory(variant_id=variant.id, warehouse_type=warehouse_for_segment(code), quantity=100))
    await db.commit()
    return variant


@pytest.fixture
async def cart(db_session: AsyncSession, segments) -> dict[str, ProductVariant]:
    return {
        "on": await _product(db_session, segments, DEALER_PRICE["on"], switch=True),
        "off": await _product(db_session, segments, DEALER_PRICE["off"], switch=False),
        "big": await _product(db_session, segments, DEALER_PRICE["big"], switch=False),
    }


async def _firm(client: AsyncClient, db: AsyncSession, segments, segment: SegmentType) -> dict[str, str]:
    """A tobacco shop that signed up through the form, then put on a price list by the admin."""
    email = f"bayi-{uuid.uuid4().hex[:8]}@test.com"
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


D = Decimal


async def test_a_bayi_paying_by_transfer_gets_it_on_switched_on_lines_only(client: AsyncClient, db_session, segments, cart):
    bayi = await _firm(client, db_session, segments, SegmentType.DEALER)
    quote = await _quote(client, bayi, [(cart["on"], 2), (cart["off"], 1)])

    # Bayi prices: 2 × 1,000 + 1 × 1,500.
    assert D(quote["subtotal"]) == D("3500.00")
    assert quote["perks_eligible"] is False  # still no retail perks
    on, off = _line(quote, cart["on"]), _line(quote, cart["off"])
    assert on["dealer_havale"] is True and D(on["havale_discount"]) == D("100.00")  # 5% of 2,000
    assert off["dealer_havale"] is False and D(off["havale_discount"]) == D("0.00")
    assert D(quote["discount_rate"]) == D("0.05")
    assert D(quote["discount_amount"]) == D("100.00")  # not 5% of the whole 3,500
    assert D(quote["volume_discount_amount"]) == D("0.00")
    assert D(quote["net_total"]) == D("3400.00")

    # The invoice: lines before VAT, the discount before VAT, VAT on what is left.
    assert D(quote["subtotal_excl_tax"]) == D("2916.67")  # 1,666.67 + 1,250.00
    assert D(quote["line_discount_excl_tax"]) == D("83.34")  # 1,666.67 − 1,583.33
    assert D(quote["vat_total"]) == D("566.67")  # 3,400 − 2,833.33
    assert (
        D(quote["subtotal_excl_tax"]) - D(quote["line_discount_excl_tax"]) + D(quote["vat_total"])
        == D(quote["net_total"])
    )


async def test_by_card_nothing_comes_off_but_the_line_says_it_would(client: AsyncClient, db_session, segments, cart):
    bayi = await _firm(client, db_session, segments, SegmentType.DEALER)
    quote = await _quote(client, bayi, [(cart["on"], 2), (cart["off"], 1)], payment="CREDIT_CARD")
    assert D(quote["discount_amount"]) == D("0.00") and D(quote["discount_rate"]) == D("0.00")
    assert D(quote["net_total"]) == D("3500.00")
    assert _line(quote, cart["on"])["dealer_havale"] is True
    assert D(_line(quote, cart["on"])["havale_discount"]) == D("0.00")


async def test_the_rate_is_the_tier_of_the_whole_cart(client: AsyncClient, db_session, segments, cart):
    bayi = await _firm(client, db_session, segments, SegmentType.DEALER)
    # 2,000 switched on + 30,000 switched off: the cart reaches the 7% tier,
    # and 7% is taken off the switched-on 2,000 alone.
    quote = await _quote(client, bayi, [(cart["on"], 2), (cart["big"], 2)])
    assert D(quote["subtotal"]) == D("32000.00")
    assert D(quote["discount_rate"]) == D("0.07")
    assert D(_line(quote, cart["on"])["havale_discount"]) == D("140.00")
    assert D(_line(quote, cart["big"])["havale_discount"]) == D("0.00")
    assert D(quote["discount_amount"]) == D("140.00")
    assert D(quote["net_total"]) == D("31860.00")


async def test_without_a_switched_on_line_a_bayi_pays_in_full(client: AsyncClient, db_session, segments, cart):
    bayi = await _firm(client, db_session, segments, SegmentType.DEALER)
    quote = await _quote(client, bayi, [(cart["off"], 3)])
    assert D(quote["discount_amount"]) == D("0.00") and D(quote["net_total"]) == D("4500.00")
    assert D(quote["line_discount_excl_tax"]) == D("0.00")


async def test_only_a_bayi_gets_it(client: AsyncClient, db_session, segments, cart):
    toptan = await _firm(client, db_session, segments, SegmentType.WHOLESALE)
    quote = await _quote(client, toptan, [(cart["on"], 2), (cart["off"], 1)])
    assert D(quote["discount_amount"]) == D("0.00")
    assert all(not line["dealer_havale"] for line in quote["items"])


async def test_retail_keeps_its_cart_wide_discount(client: AsyncClient, customer_user: User, cart):
    quote = await _quote(client, auth_header(customer_user), [(cart["on"], 2), (cart["off"], 1)])
    # Retail prices (×1.5): 3,000 + 2,250. The whole cart takes 5%, switch or not.
    assert D(quote["subtotal"]) == D("5250.00")
    assert D(quote["discount_amount"]) == D("262.50")
    assert all(not line["dealer_havale"] and D(line["havale_discount"]) == 0 for line in quote["items"])


async def test_the_order_records_the_bayi_discount(client: AsyncClient, db_session, segments, cart):
    bayi = await _firm(client, db_session, segments, SegmentType.DEALER)
    resp = await client.post("/api/v1/orders/checkout", headers=bayi, json=_body([(cart["on"], 2), (cart["off"], 1)]))
    assert resp.status_code == 201, resp.text
    order = resp.json()
    assert D(order["total_amount"]) == D("3500.00")
    assert D(order["discount_amount"]) == D("100.00")
    assert order["payment_method"] == "BANK_TRANSFER"
