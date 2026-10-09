"""The toptan/kafe checkout: five ways to be delivered, and the İstanbul limit.

A firm picks one of five cards at checkout, and each one leaves its own trace on
the order: its invoice address, a branch of its own, Yurtiçi with its own
agreement number, a nakliye ambarı it names, or the shop's own van. The first
four are destinations and carriers; the fifth is a price, and the price is a
setting — "İstanbul içi 20000 ve üzeri alışverişlerde ücretsiz teslimat", with
the 20,000 editable from the admin panel.

So these tests walk the limit from both sides, change it in the panel, and walk
it again. The two agreements are carried throughout: a firm's order is refused
without them exactly as a person's is.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice, WarehouseType
from app.models.settings import ShopSetting
from app.models.user import CustomerSegment, SegmentType, User, UserRole
from app.services.checkout_engine import warehouse_for_segment
from app.services.shop_settings import ISTANBUL_FREE_MIN_DEFAULT, ISTANBUL_FREE_MIN_KEY
from tests.conftest import CONSENTS, auth_header

# ₺10,000 a unit, so one unit is below the ₺20,000 limit and two are exactly on it.
UNIT_PRICE = Decimal("10000.00")

CORPORATE = {
    "entity_type": "COMPANY",
    "title": "Köşe Kafe Ltd.",
    "tax_plate_name": "Köşe Kafe Ltd.",
    "tax_office": "Beşiktaş",
    "tax_number": "1234567890",
}
ISTANBUL_REGISTERED = {
    "neighbourhood": "Caferağa Mah.",
    "street": "Moda Cad.",
    "building_no": "7",
    "district": "Kadıköy",
    "city": "İstanbul",
}
ANTALYA_REGISTERED = {
    "neighbourhood": "Kaleiçi Mah.",
    "street": "Atatürk Cad.",
    "building_no": "12",
    "district": "Muratpaşa",
    "city": "Antalya",
}
# The registered place, written out as checkout sends it.
ISTANBUL_ADDRESS = {
    "full_name": "Mehmet Demir",
    "phone": "0532 444 55 66",
    "country": "TR",
    "city": "İstanbul",
    "district": "Kadıköy",
    "line": "Caferağa Mah., Moda Caddesi No: 7, Kapı No: 3",
}
# A branch of the same firm, two cities away: only "Şubeme istiyorum" reaches it.
BRANCH_ADDRESS = {
    "full_name": "Ayşe Yıldız",
    "phone": "0216 555 44 33",
    "country": "TR",
    "city": "Bursa",
    "district": "Nilüfer",
    "line": "Fethiye Mah., Şube Sokak No: 4",
}


async def _firm(
    client: AsyncClient, *, city: dict | None = None, account_type: str = "CAFE"
) -> dict[str, str]:
    """A firm that signed up through the form, so it has an address on file."""
    email = f"b2b-{uuid.uuid4().hex[:8]}@test.com"
    body = {
        "account_type": account_type,
        "email": email,
        "password": "Gizli-Parola-123",
        "contact_name": "Mehmet Demir",
        "phone": "0532 444 55 66",
        "company": CORPORATE,
        "billing_address": city or ISTANBUL_REGISTERED,
    }
    if account_type == "INTERNATIONAL":
        # Nothing Turkish applies abroad: no vergi dairesi, no VKN.
        body["international_kind"] = "WHOLESALER"
        body["company"] = {"entity_type": "COMPANY", "title": "Berlin Shisha GmbH"}
    resp = await client.post("/api/v1/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "Gizli-Parola-123"}
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _variant(
    db: AsyncSession, segments: dict[SegmentType, CustomerSegment]
) -> ProductVariant:
    """One product priced the same on every list, stocked in every warehouse."""
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"B2B-{suffix}", name=f"B2B Testi {suffix}", tags=[])
    db.add(product)
    await db.flush()
    variant = ProductVariant(product_id=product.id, sku=f"B2B-{suffix}")
    db.add(variant)
    await db.flush()
    for code in SegmentType:
        db.add(VariantPrice(
            variant_id=variant.id, segment_id=segments[code].id, currency="TRY",
            price_excl_tax=(UNIT_PRICE / Decimal("1.20")).quantize(Decimal("0.01")),
            price_incl_tax=UNIT_PRICE, vat_rate=Decimal("20.00"),
        ))
        db.add(Inventory(
            variant_id=variant.id, warehouse_type=warehouse_for_segment(code), quantity=50,
        ))
    await db.commit()
    return variant


def _body(variant: ProductVariant, quantity: int = 1, **extra) -> dict:
    return {
        **CONSENTS,
        "payment_method": "BANK_TRANSFER",
        "items": [{"variant_id": str(variant.id), "quantity": quantity}],
        "shipping_method": "YURTICI_DOOR",
        "shipping_consent": True,
        "shipping_address": ISTANBUL_ADDRESS,
        **extra,
    }


async def _checkout(client: AsyncClient, headers: dict, variant: ProductVariant, **extra):
    return await client.post("/api/v1/orders/checkout", headers=headers, json=_body(variant, **extra))


async def _quote(client: AsyncClient, headers: dict, variant: ProductVariant, **extra):
    body = _body(variant, **extra)
    body.pop("shipping_address", None)
    return await client.post("/api/v1/orders/calculate-cart", headers=headers, json=body)


# ── The five cards ───────────────────────────────────────────────────────────


async def test_the_default_card_delivers_to_the_registered_invoice_address(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """"Fatura adresime istiyorum": the card the cart opens on."""
    headers = await _firm(client)
    variant = await _variant(db_session, segments)
    placed = await _checkout(client, headers, variant)
    assert placed.status_code == 201, placed.text
    body = placed.json()
    assert body["shipping_method"] == "YURTICI_DOOR"
    assert body["shipping_branch_name"] is None
    assert body["shipping_address"]["city"] == "İstanbul"


async def test_a_firm_can_send_an_order_to_its_own_branch(
    client: AsyncClient, db_session: AsyncSession, segments, admin_user: User
):
    """"Şubeme istiyorum": the branch is named, and its address is taken as given
    even though it is nowhere near the registered one."""
    headers = await _firm(client)
    variant = await _variant(db_session, segments)
    placed = await _checkout(
        client, headers, variant,
        shipping_address=BRANCH_ADDRESS,
        shipping_branch_name="  Nilüfer   Şubesi  ",
    )
    assert placed.status_code == 201, placed.text
    body = placed.json()
    assert body["shipping_branch_name"] == "Nilüfer Şubesi"
    assert (body["shipping_address"]["city"], body["shipping_address"]["phone"]) == (
        "Bursa", "0216 555 44 33",
    )

    # The shop can see which branch to send it to.
    sheet = await client.get("/api/v1/admin/orders", headers=auth_header(admin_user))
    row = next(o for o in sheet.json() if o["id"] == body["id"])
    assert row["shipping_branch_name"] == "Nilüfer Şubesi"


async def test_the_same_address_without_a_branch_name_is_still_refused(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """The branch is what opens the door, not the address: a firm that has not
    said "Şubeme istiyorum" still ships where it is registered."""
    headers = await _firm(client)
    variant = await _variant(db_session, segments)
    refused = await _checkout(client, headers, variant, shipping_address=BRANCH_ADDRESS)
    assert refused.status_code == 422, refused.text
    assert "kayıtlı fatura adresiniz" in refused.json()["detail"]


async def test_a_branch_needs_a_name_worth_reading(
    client: AsyncClient, db_session: AsyncSession, segments
):
    headers = await _firm(client)
    variant = await _variant(db_session, segments)
    refused = await _checkout(
        client, headers, variant, shipping_address=BRANCH_ADDRESS, shipping_branch_name="K",
    )
    assert refused.status_code == 422, refused.text


async def test_a_firm_keeps_its_yurtici_agreement_number(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """"Yurtiçi Kargo adres teslim istiyorum" with "size ait anlaşma varsa"."""
    headers = await _firm(client)
    variant = await _variant(db_session, segments)
    placed = await _checkout(client, headers, variant, shipping_contract_no=" YK-10234 ")
    assert placed.status_code == 201, placed.text
    assert placed.json()["shipping_contract_no"] == "YK-10234"


async def test_a_firm_can_name_the_ambar_and_its_phone(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """"Nakliye ambarı ile teslimat istiyorum" → "Ambar bilgisi girebilirsiniz"."""
    headers = await _firm(client)
    variant = await _variant(db_session, segments)
    placed = await _checkout(
        client, headers, variant, shipping_method="NAKLIYE_AMBARI",
        freight_company_name="Nilüfer  Ambarı", freight_phone="0224 000 00 00",
    )
    assert placed.status_code == 201, placed.text
    body = placed.json()
    assert (body["freight_company_name"], body["freight_phone"]) == (
        "Nilüfer Ambarı", "0224 000 00 00",
    )
    assert body["shipping_payer"] == "RECEIVER"


async def test_a_branch_order_can_go_by_ambar_too(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """A branch is where it goes, not how: the two travel together."""
    headers = await _firm(client)
    variant = await _variant(db_session, segments)
    placed = await _checkout(
        client, headers, variant, shipping_method="NAKLIYE_AMBARI",
        shipping_address=BRANCH_ADDRESS, shipping_branch_name="Nilüfer Şubesi",
    )
    assert placed.status_code == 201, placed.text
    assert placed.json()["shipping_branch_name"] == "Nilüfer Şubesi"


async def test_an_export_order_has_no_branch(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """Freight is quoted against the address it was costed on, so a branch name
    is dropped rather than honoured."""
    headers = await _firm(client, account_type="INTERNATIONAL", city={
        "country": "DE", "city": "Berlin", "postal_code": "10115",
        "address_line1": "Hauptstraße 1",
    })
    variant = await _variant(db_session, segments)
    placed = await _checkout(
        client, headers, variant, shipping_method="INTERNATIONAL_FREIGHT",
        shipping_branch_name="Berlin Şubesi",
        shipping_address={
            "full_name": "John Smith", "phone": "+49 30 1234 5678", "country": "DE",
            "city": "Berlin", "postal_code": "10115", "line": "Hauptstraße 1, Apt 5",
        },
    )
    assert placed.status_code == 201, placed.text
    assert placed.json()["shipping_branch_name"] is None


# ── The İstanbul limit ───────────────────────────────────────────────────────


async def test_the_van_is_free_from_the_limit_upwards(
    client: AsyncClient, db_session: AsyncSession, segments
):
    headers = await _firm(client)
    variant = await _variant(db_session, segments)
    quote = await _quote(client, headers, variant, quantity=2, shipping_method="ISTANBUL_FREE")
    assert quote.status_code == 200, quote.text
    assert Decimal(quote.json()["subtotal"]) == ISTANBUL_FREE_MIN_DEFAULT
    assert quote.json()["shipping"]["reason"] == "free"

    placed = await _checkout(client, headers, variant, quantity=2, shipping_method="ISTANBUL_FREE")
    assert placed.status_code == 201, placed.text
    assert placed.json()["shipping_payer"] == "SHOP"


async def test_below_the_limit_the_van_is_not_an_option(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """And the refusal says what the limit is and what the cart holds, so the
    cart can tell the customer how much more to add."""
    headers = await _firm(client)
    variant = await _variant(db_session, segments)
    refused = await _quote(client, headers, variant, quantity=1, shipping_method="ISTANBUL_FREE")
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    assert "20.000,00 TL" in detail and "10.000,00 TL" in detail


async def test_a_firm_outside_istanbul_is_refused_whatever_the_cart_is_worth(
    client: AsyncClient, db_session: AsyncSession, segments
):
    headers = await _firm(client, city=ANTALYA_REGISTERED)
    variant = await _variant(db_session, segments)
    refused = await _quote(client, headers, variant, quantity=5, shipping_method="ISTANBUL_FREE")
    assert refused.status_code == 422, refused.text
    assert "kayıtlı adresler" in refused.json()["detail"]


# ── The limit as a setting ───────────────────────────────────────────────────


async def test_the_limit_is_published_without_a_login(client: AsyncClient):
    resp = await client.get("/api/v1/settings/shipping")
    assert resp.status_code == 200, resp.text
    assert Decimal(resp.json()["istanbul_free_min_subtotal"]) == ISTANBUL_FREE_MIN_DEFAULT


async def test_an_admin_lowers_the_limit_and_checkout_follows(
    client: AsyncClient, db_session: AsyncSession, segments, admin_user: User
):
    """The whole point of the setting: one cart, refused and then accepted,
    with nothing changed but the number in the panel."""
    headers = await _firm(client)
    variant = await _variant(db_session, segments)
    assert (await _quote(
        client, headers, variant, quantity=1, shipping_method="ISTANBUL_FREE"
    )).status_code == 422

    saved = await client.put(
        "/api/v1/admin/settings/shipping",
        headers=auth_header(admin_user),
        json={"istanbul_free_min_subtotal": "5000"},
    )
    assert saved.status_code == 200, saved.text
    assert Decimal(saved.json()["istanbul_free_min_subtotal"]) == Decimal("5000.00")

    accepted = await _quote(client, headers, variant, quantity=1, shipping_method="ISTANBUL_FREE")
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["shipping"]["reason"] == "free"
    # And the storefront is told the same figure.
    public = await client.get("/api/v1/settings/shipping")
    assert Decimal(public.json()["istanbul_free_min_subtotal"]) == Decimal("5000.00")

    # Raising it again closes the option on the same cart.
    await client.put(
        "/api/v1/admin/settings/shipping",
        headers=auth_header(admin_user),
        json={"istanbul_free_min_subtotal": "30000.00"},
    )
    assert (await _quote(
        client, headers, variant, quantity=2, shipping_method="ISTANBUL_FREE"
    )).status_code == 422


async def test_the_saved_limit_records_who_changed_it(
    client: AsyncClient, db_session: AsyncSession, admin_user: User
):
    await client.put(
        "/api/v1/admin/settings/shipping",
        headers=auth_header(admin_user),
        json={"istanbul_free_min_subtotal": "12500.50"},
    )
    row = await db_session.get(ShopSetting, ISTANBUL_FREE_MIN_KEY)
    await db_session.refresh(row)
    assert (row.value, row.updated_by) == ("12500.50", admin_user.id)


@pytest.mark.parametrize("value", ["-1", "2000000", "abc"])
async def test_an_impossible_limit_is_refused(
    client: AsyncClient, admin_user: User, value: str
):
    resp = await client.put(
        "/api/v1/admin/settings/shipping",
        headers=auth_header(admin_user),
        json={"istanbul_free_min_subtotal": value},
    )
    assert resp.status_code == 422, resp.text


async def test_a_customer_cannot_change_the_limit(
    client: AsyncClient, db_session: AsyncSession, segments
):
    headers = await _firm(client)
    resp = await client.put(
        "/api/v1/admin/settings/shipping", headers=headers,
        json={"istanbul_free_min_subtotal": "0"},
    )
    assert resp.status_code == 403, resp.text
    assert (await client.get("/api/v1/admin/settings/shipping", headers=headers)).status_code == 403


async def test_an_unreadable_stored_limit_falls_back_to_the_default(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """A hand-edited row cannot take the cart down with it."""
    row = await db_session.get(ShopSetting, ISTANBUL_FREE_MIN_KEY)
    if row is None:
        row = ShopSetting(key=ISTANBUL_FREE_MIN_KEY)
        db_session.add(row)
    row.value = "yirmi bin"
    await db_session.commit()

    resp = await client.get("/api/v1/settings/shipping")
    assert Decimal(resp.json()["istanbul_free_min_subtotal"]) == ISTANBUL_FREE_MIN_DEFAULT


# ── The gate, for a firm too ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "missing", [{"distance_sales_accepted": False}, {"terms_accepted": False}]
)
async def test_a_firms_order_needs_both_agreements(
    client: AsyncClient, db_session: AsyncSession, segments, missing: dict
):
    headers = await _firm(client)
    variant = await _variant(db_session, segments)
    refused = await _checkout(client, headers, variant, **missing)
    assert refused.status_code == 422, refused.text
    assert "Sözleşme" in refused.json()["detail"] or "Sözleşmesi" in refused.json()["detail"]


async def test_a_firm_can_ask_for_its_card_to_be_saved(
    client: AsyncClient, db_session: AsyncSession, segments
):
    headers = await _firm(client)
    variant = await _variant(db_session, segments)
    placed = await _checkout(client, headers, variant, payment_method="CREDIT_CARD", save_card=True)
    assert placed.status_code == 201, placed.text
    assert placed.json()["save_card"] is True
    assert placed.json()["agreements_accepted_at"] is not None
