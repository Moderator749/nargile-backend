"""The checkout funnel: who gets the retail perks, and how an order travels.

The money rules first. ParaPuan, the ciro discount and the havale discount are
retail promotions: a bireysel account on the retail price list gets all three,
and nobody else gets any of them — a kafe, a shop, an export customer, or a
bireysel account an admin has moved onto a B2B tier. Those pay their tier price
net, and the tests below walk the same cart across the boundary to prove the
only thing that changed is who was buying.

Then delivery: each kind of account is offered its own shipping methods and
refused the others, free shipping is one of the retail promotions rather than a
carrier setting, a firm ships to the address it registered, and an order
remembers who signs for it.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice, WarehouseType
from app.models.loyalty import LoyaltyLedger, PointSource, TransactionType
from app.models.order import Order, OrderStatus, PaymentMethod
from app.models.user import AccountType, CustomerSegment, SegmentType, User, UserRole
from app.services.checkout_engine import warehouse_for_segment
from app.services.loyalty_service import get_user_loyalty_balance
from tests.conftest import ADDRESS, CONSENTS, auth_header

# ₺40,000 reaches the 5% ciro tier and the 7% havale tier, so one cart exercises
# both discounts at once. ParaPuan is earned in its own band (₺20,000–29,999.99,
# see app.services.wallet), so the earning tests buy less.
UNIT_PRICE = Decimal("10000.00")
QUANTITY = 4
SUBTOTAL = UNIT_PRICE * QUANTITY
EARNING_QUANTITY = 2

ABROAD = {
    "full_name": "John Smith",
    "phone": "+49 30 1234 5678",
    "country": "DE",
    "city": "Berlin",
    "postal_code": "10115",
    "line": "Hauptstraße 1, Apt 5",
}


async def _buyer(
    db: AsyncSession,
    segments: dict[SegmentType, CustomerSegment],
    account_type: AccountType = AccountType.INDIVIDUAL,
    segment: SegmentType = SegmentType.RETAIL,
) -> User:
    user = User(
        email=f"perk-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        full_name="Test Alıcı",
        phone="0532 111 22 33",
        role=UserRole.CUSTOMER,
        account_type=account_type,
        segment_id=segments[segment].id,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _variant(
    db: AsyncSession, segments: dict[SegmentType, CustomerSegment], quota: int = 50
) -> ProductVariant:
    """One product priced the same on every list, so a changed total can only be
    a rule and never a different price."""
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"PRK-{suffix}", name=f"Perk Testi {suffix}", tags=[])
    db.add(product)
    await db.flush()
    variant = ProductVariant(product_id=product.id, sku=f"PRK-{suffix}")
    db.add(variant)
    await db.flush()
    for code in SegmentType:
        db.add(VariantPrice(
            variant_id=variant.id, segment_id=segments[code].id, currency="TRY",
            price_excl_tax=(UNIT_PRICE / Decimal("1.20")).quantize(Decimal("0.01")),
            price_incl_tax=UNIT_PRICE, vat_rate=Decimal("20.00"),
        ))
        db.add(Inventory(variant_id=variant.id, warehouse_type=warehouse_for_segment(code), quantity=quota))
    await db.commit()
    return variant


async def _grant_points(db: AsyncSession, user: User, amount: int) -> None:
    now = datetime.now(timezone.utc)
    db.add(LoyaltyLedger(
        user_id=user.id, amount=amount, transaction_type=TransactionType.EARNED,
        source=PointSource.ADMIN, created_at=now, expires_at=now + timedelta(days=365),
    ))
    await db.commit()


def method_for(user: User) -> str:
    """The one method every account has: its door. An export order has exactly one."""
    return (
        "INTERNATIONAL_FREIGHT"
        if user.account_type == AccountType.INTERNATIONAL
        else "YURTICI_DOOR"
    )


def _body(variant: ProductVariant, user: User | None = None, **extra) -> dict:
    body = {
        "payment_method": "BANK_TRANSFER",
        "items": [{"variant_id": str(variant.id), "quantity": QUANTITY}],
        **CONSENTS,
    }
    if user is not None:
        body["shipping_method"] = method_for(user)
    return {**body, **extra}


async def _quote(client: AsyncClient, user: User, variant: ProductVariant, **extra) -> dict:
    resp = await client.post(
        "/api/v1/orders/calculate-cart", headers=auth_header(user), json=_body(variant, user, **extra)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── The perk boundary ────────────────────────────────────────────────────────


async def test_a_bireysel_retail_cart_gets_all_three_promotions(
    client: AsyncClient, db_session: AsyncSession, segments
):
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    await _grant_points(db_session, user, 500)

    quote = await _quote(client, user, variant, points_to_redeem=500)
    assert quote["perks_eligible"] is True
    assert Decimal(quote["subtotal"]) == SUBTOTAL
    # 5% ciro on 40,000, then 7% havale on what is left, then points at ₺1.
    assert Decimal(quote["volume_discount_amount"]) == Decimal("2000.00")
    assert Decimal(quote["discount_amount"]) == Decimal("2660.00")
    assert Decimal(quote["points_discount"]) == Decimal("500.00")
    assert Decimal(quote["net_total"]) == Decimal("34840.00")


@pytest.mark.parametrize(
    ("account_type", "segment"),
    [
        pytest.param(AccountType.CAFE, SegmentType.CAFE, id="a kafe on the kafe list"),
        pytest.param(AccountType.TOBACCO_SHOP, SegmentType.DEALER, id="a shop on the bayi list"),
        pytest.param(AccountType.TOBACCO_SHOP, SegmentType.WHOLESALE, id="a shop on the toptan list"),
        pytest.param(AccountType.INTERNATIONAL, SegmentType.RETAIL, id="an export customer"),
        pytest.param(AccountType.CAFE, SegmentType.RETAIL, id="a kafe still on retail prices"),
        pytest.param(AccountType.INDIVIDUAL, SegmentType.DEALER, id="a bireysel moved to the bayi list"),
    ],
)
async def test_everyone_else_pays_their_tier_price_net(
    client: AsyncClient, db_session: AsyncSession, segments, account_type: AccountType, segment: SegmentType
):
    """The same cart, the same prices: only the buyer differs."""
    user = await _buyer(db_session, segments, account_type, segment)
    variant = await _variant(db_session, segments)

    quote = await _quote(client, user, variant)
    assert quote["perks_eligible"] is False
    assert Decimal(quote["volume_discount_amount"]) == Decimal("0.00")
    assert Decimal(quote["discount_amount"]) == Decimal("0.00")
    assert Decimal(quote["points_discount"]) == Decimal("0.00")
    # Nothing off, nothing earned: the subtotal is the total.
    assert Decimal(quote["net_total"]) == SUBTOTAL
    assert quote["loyalty_points_to_earn"] == 0


@pytest.mark.parametrize("method", ["BANK_TRANSFER", "CREDIT_CARD"])
async def test_how_a_b2b_account_pays_changes_nothing(
    client: AsyncClient, db_session: AsyncSession, segments, method: str
):
    """The havale discount is a retail promotion, so a kafe pays the same either way."""
    user = await _buyer(db_session, segments, AccountType.CAFE, SegmentType.CAFE)
    variant = await _variant(db_session, segments)
    quote = await _quote(client, user, variant, payment_method=method)
    assert Decimal(quote["net_total"]) == SUBTOTAL


async def test_a_b2b_account_asking_to_spend_points_is_refused(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """Not silently ignored: a cart that would have been cheaper is an error."""
    user = await _buyer(db_session, segments, AccountType.CAFE, SegmentType.CAFE)
    variant = await _variant(db_session, segments)
    await _grant_points(db_session, user, 500)

    resp = await client.post(
        "/api/v1/orders/calculate-cart",
        headers=auth_header(user),
        json=_body(variant, points_to_redeem=500),
    )
    assert resp.status_code == 422, resp.text
    assert "bireysel" in resp.json()["detail"]


async def test_points_a_b2b_account_already_had_stay_in_the_wallet(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """Earned as a bireysel, kept after an upgrade: unspendable, not confiscated."""
    user = await _buyer(db_session, segments, AccountType.INDIVIDUAL, SegmentType.DEALER)
    await _grant_points(db_session, user, 750)
    assert await get_user_loyalty_balance(db_session, user.id) == 750


@pytest.mark.parametrize(
    ("account_type", "segment", "earns"),
    [
        pytest.param(AccountType.INDIVIDUAL, SegmentType.RETAIL, True, id="a bireysel order earns"),
        pytest.param(AccountType.CAFE, SegmentType.CAFE, False, id="a kafe order earns nothing"),
        pytest.param(
            AccountType.INDIVIDUAL, SegmentType.DEALER, False, id="a bireysel on the bayi list earns nothing"
        ),
    ],
)
async def test_points_are_granted_on_approval_only_to_a_retail_buyer(
    client: AsyncClient, db_session: AsyncSession, segments, admin_user: User,
    account_type: AccountType, segment: SegmentType, earns: bool,
):
    """The same ₺20,000 order — inside the ParaPuan band — approved the same way."""
    user = await _buyer(db_session, segments, account_type, segment)
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(user),
        json={**CONSENTS, 
            "payment_method": "CREDIT_CARD",
            "items": [{"variant_id": str(variant.id), "quantity": EARNING_QUANTITY}],
            "shipping_method": method_for(user),
            "shipping_address": ADDRESS,
            "shipping_consent": True,
        },
    )
    assert placed.status_code == 201, placed.text

    approved = await client.post(
        f"/api/v1/admin/orders/{placed.json()['id']}/approve", headers=auth_header(admin_user), json={}
    )
    assert approved.status_code == 200, approved.text
    awarded = approved.json()["loyalty_points_awarded"]
    balance = await get_user_loyalty_balance(db_session, user.id)
    assert (awarded > 0, balance > 0) == (earns, earns)


# ── Free shipping is a retail promotion too ──────────────────────────────────


async def test_a_retail_cart_over_the_threshold_ships_on_the_shop(
    client: AsyncClient, db_session: AsyncSession, segments
):
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    quote = await _quote(client, user, variant)
    assert Decimal(quote["subtotal"]) > Decimal(quote["shipping"]["free_threshold"])
    assert (quote["shipping"]["reason"], quote["shipping"]["free"]) == ("free", True)


@pytest.mark.parametrize(
    ("account_type", "segment"),
    [
        pytest.param(AccountType.CAFE, SegmentType.CAFE, id="a kafe"),
        pytest.param(AccountType.TOBACCO_SHOP, SegmentType.DEALER, id="a shop"),
        pytest.param(AccountType.INDIVIDUAL, SegmentType.WHOLESALE, id="a bireysel on a B2B list"),
    ],
)
async def test_a_b2b_cart_never_reaches_free_shipping(
    client: AsyncClient, db_session: AsyncSession, segments,
    account_type: AccountType, segment: SegmentType,
):
    """₺40,000 — well over the threshold — and still karşı ödemeli: a tier price
    does not include carriage."""
    user = await _buyer(db_session, segments, account_type, segment)
    variant = await _variant(db_session, segments)
    quote = await _quote(client, user, variant, shipping_method="YURTICI_DOOR")
    assert Decimal(quote["subtotal"]) > Decimal(quote["shipping"]["free_threshold"])
    assert quote["shipping"]["reason"] == "tier_price"
    assert (quote["shipping"]["free"], quote["shipping"]["receiver_pays"]) == (False, True)
    # Never charged on the order either way; it is paid to the carrier.
    assert Decimal(quote["cargo_cost"]) == Decimal("0.00")
    assert Decimal(quote["net_total"]) == SUBTOTAL


async def test_for_a_firm_its_door_is_the_same_deal_as_a_freight_depot(
    client: AsyncClient, db_session: AsyncSession, segments
):
    user = await _buyer(db_session, segments, AccountType.CAFE, SegmentType.CAFE)
    variant = await _variant(db_session, segments)
    cargo = await _quote(client, user, variant, shipping_method="YURTICI_DOOR")
    freight = await _quote(client, user, variant, shipping_method="NAKLIYE_AMBARI")
    for key in ("free", "receiver_pays"):
        assert cargo["shipping"][key] == freight["shipping"][key] == (key == "receiver_pays")
    assert Decimal(cargo["net_total"]) == Decimal(freight["net_total"]) == SUBTOTAL


async def test_a_firms_door_order_is_recorded_as_receiver_paid(
    client: AsyncClient, db_session: AsyncSession, segments
):
    user = await _buyer(db_session, segments, AccountType.CAFE, SegmentType.CAFE)
    variant = await _variant(db_session, segments)
    body = _body(variant, user, shipping_address=ADDRESS)

    refused = await client.post("/api/v1/orders/checkout", headers=auth_header(user), json=body)
    assert refused.status_code == 422, "carriage it does not cover still needs consent"

    placed = await client.post(
        "/api/v1/orders/checkout", headers=auth_header(user), json={**CONSENTS, **body, "shipping_consent": True}
    )
    assert placed.status_code == 201, placed.text
    assert placed.json()["shipping_payer"] == "RECEIVER"
    assert Decimal(placed.json()["shipping_cost"]) == Decimal("0.00")


# ── Shipping methods, by account type ────────────────────────────────────────


@pytest.mark.parametrize(
    ("account_type", "allowed", "refused"),
    [
        pytest.param(
            AccountType.INDIVIDUAL,
            ["YURTICI_DOOR", "YURTICI_BRANCH"],
            ["NAKLIYE_AMBARI", "ISTANBUL_FREE", "INTERNATIONAL_FREIGHT"],
            id="bireysel: to the door or to a branch",
        ),
        pytest.param(
            AccountType.CAFE,
            ["YURTICI_DOOR", "NAKLIYE_AMBARI"],
            ["YURTICI_BRANCH", "INTERNATIONAL_FREIGHT"],
            id="kafe: its door or a nakliye ambarı",
        ),
        pytest.param(
            AccountType.TOBACCO_SHOP,
            ["YURTICI_DOOR", "NAKLIYE_AMBARI"],
            ["YURTICI_BRANCH", "INTERNATIONAL_FREIGHT"],
            id="tobacco shop: its door or a nakliye ambarı",
        ),
        pytest.param(
            AccountType.INTERNATIONAL,
            ["INTERNATIONAL_FREIGHT"],
            ["YURTICI_DOOR", "YURTICI_BRANCH", "NAKLIYE_AMBARI", "ISTANBUL_FREE"],
            id="abroad: export only",
        ),
    ],
)
async def test_each_account_is_offered_its_own_shipping_methods(
    client: AsyncClient, db_session: AsyncSession, segments,
    account_type: AccountType, allowed: list[str], refused: list[str],
):
    user = await _buyer(db_session, segments, account_type, SegmentType.RETAIL)
    variant = await _variant(db_session, segments)
    for method in allowed:
        quote = await _quote(client, user, variant, shipping_method=method)
        assert quote["shipping"]["reason"], method
    for method in refused:
        resp = await client.post(
            "/api/v1/orders/calculate-cart",
            headers=auth_header(user),
            json=_body(variant, shipping_method=method),
        )
        assert resp.status_code == 422, f"{method} should not be offered: {resp.text}"
        assert "kargo seçeneği" in resp.json()["detail"]


async def test_a_branch_collection_ships_on_the_shop_like_any_parcel(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """It is the same shipment, so the same ₺3,000 rule covers it."""
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    quote = await _quote(client, user, variant, shipping_method="YURTICI_BRANCH")
    assert quote["shipping"]["reason"] == "free"
    assert quote["shipping"]["free"] is True


@pytest.mark.parametrize(
    ("account_type", "method", "reason"),
    [
        pytest.param(
            AccountType.CAFE, "NAKLIYE_AMBARI", "freight", id="a freight depot collects its own fee"
        ),
        pytest.param(
            AccountType.INTERNATIONAL, "INTERNATIONAL_FREIGHT", "international", id="export: quoted later"
        ),
    ],
)
async def test_what_the_shop_cannot_quote_it_does_not_cover(
    client: AsyncClient, db_session: AsyncSession, segments,
    account_type: AccountType, method: str, reason: str,
):
    user = await _buyer(db_session, segments, account_type, SegmentType.RETAIL)
    variant = await _variant(db_session, segments)
    quote = await _quote(client, user, variant, shipping_method=method)
    # Well past the free-shipping threshold, and still not on the shop.
    assert Decimal(quote["subtotal"]) == SUBTOTAL
    assert quote["shipping"]["reason"] == reason
    assert (quote["shipping"]["free"], quote["shipping"]["receiver_pays"]) == (False, True)
    assert Decimal(quote["cargo_cost"]) == Decimal("0.00")


@pytest.mark.parametrize(
    ("account_type", "method", "address"),
    [
        pytest.param(AccountType.CAFE, "NAKLIYE_AMBARI", ADDRESS, id="freight"),
        pytest.param(AccountType.INTERNATIONAL, "INTERNATIONAL_FREIGHT", ABROAD, id="export"),
    ],
)
async def test_a_shipment_the_shop_does_not_cover_needs_the_buyers_consent(
    client: AsyncClient, db_session: AsyncSession, segments,
    account_type: AccountType, method: str, address: dict,
):
    user = await _buyer(db_session, segments, account_type, SegmentType.RETAIL)
    variant = await _variant(db_session, segments)
    body = _body(variant, shipping_address=address, shipping_method=method)

    refused = await client.post("/api/v1/orders/checkout", headers=auth_header(user), json=body)
    assert refused.status_code == 422
    assert "alıcı tarafından" in refused.json()["detail"]

    placed = await client.post(
        "/api/v1/orders/checkout", headers=auth_header(user), json={**CONSENTS, **body, "shipping_consent": True}
    )
    assert placed.status_code == 201, placed.text
    assert placed.json()["shipping_method"] == method
    assert placed.json()["shipping_payer"] == "RECEIVER"


async def test_an_export_order_keeps_its_own_countrys_address(
    client: AsyncClient, db_session: AsyncSession, segments
):
    user = await _buyer(db_session, segments, AccountType.INTERNATIONAL, SegmentType.RETAIL)
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(user),
        json=_body(
            variant, shipping_address=ABROAD, shipping_method="INTERNATIONAL_FREIGHT", shipping_consent=True
        ),
    )
    assert placed.status_code == 201, placed.text
    stored = placed.json()["shipping_address"]
    assert (stored["country"], stored["city"], stored["postal_code"]) == ("DE", "Berlin", "10115")
    # No ilçe was invented for a country that has none.
    assert stored["district"] is None


# ── Who signs for it ─────────────────────────────────────────────────────────


async def test_somebody_else_can_be_named_to_collect_the_parcel(
    client: AsyncClient, db_session: AsyncSession, segments, admin_user: User
):
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(user),
        json=_body(
            variant,
            shipping_address=ADDRESS,
            shipping_method="YURTICI_BRANCH",
            shipping_consent=True,
            receiver_name="Komşu  Ali",
            receiver_phone="0533 222 11 00",
        ),
    )
    assert placed.status_code == 201, placed.text
    assert placed.json()["receiver_name"] == "Komşu Ali"
    assert placed.json()["receiver_phone"] == "0533 222 11 00"

    # The shop sees who to expect at the counter.
    sheet = await client.get("/api/v1/admin/orders", headers=auth_header(admin_user))
    row = next(o for o in sheet.json() if o["id"] == placed.json()["id"])
    assert (row["receiver_name"], row["receiver_phone"]) == ("Komşu Ali", "0533 222 11 00")


@pytest.mark.parametrize(
    "receiver",
    [
        pytest.param({"receiver_name": "Komşu Ali"}, id="a name with no number"),
        pytest.param({"receiver_phone": "0533 222 11 00"}, id="a number with nobody to call"),
        pytest.param({"receiver_name": "A", "receiver_phone": "0533 222 11 00"}, id="barely a name"),
        pytest.param({"receiver_name": "Komşu Ali", "receiver_phone": "123"}, id="not a phone number"),
    ],
)
async def test_half_a_receiver_is_refused(
    client: AsyncClient, db_session: AsyncSession, segments, receiver: dict
):
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    resp = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(user),
        json=_body(
            variant, shipping_address=ADDRESS, shipping_consent=True, **receiver
        ),
    )
    assert resp.status_code == 422, resp.text


async def test_an_order_with_nobody_else_named_has_no_receiver(
    client: AsyncClient, db_session: AsyncSession, segments
):
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(user),
        json=_body(variant, shipping_address=ADDRESS, shipping_consent=True),
    )
    assert placed.status_code == 201, placed.text
    assert (placed.json()["receiver_name"], placed.json()["receiver_phone"]) == (None, None)


CORPORATE = {
    "entity_type": "COMPANY",
    "title": "Köşe Kafe Ltd.",
    "tax_plate_name": "Köşe Kafe Ltd.",
    "tax_office": "Beşiktaş",
    "tax_number": "1234567890",
}
REGISTERED = {
    "neighbourhood": "Kaleiçi Mah.",
    "street": "Atatürk Cad.",
    "building_no": "12",
    "district": "Muratpaşa",
    "city": "antalya",
}
# The same place, written better: a door number added, the street spelled out.
EDITED = {
    "full_name": "Mehmet Demir",
    "phone": "0532 444 55 66",
    "country": "TR",
    "city": "Antalya",
    "district": "muratpaşa",
    "line": "Kaleiçi Mah., Atatürk Caddesi No: 12, Kapı No: 7, 3. kat",
}
ELSEWHERE_ADDRESS = {**EDITED, "city": "İstanbul", "district": "Kadıköy"}


async def _registered_firm(
    client: AsyncClient, account_type: str = "CAFE", billing_address: dict | None = None
) -> tuple[str, dict]:
    """A firm that signed up through the form, so it has an address on file."""
    email = f"ship-{uuid.uuid4().hex[:8]}@test.com"
    resp = await client.post("/api/v1/auth/register", json={
        "account_type": account_type,
        "email": email,
        "password": "Gizli-Parola-123",
        "contact_name": "Mehmet Demir",
        "phone": "0532 444 55 66",
        "company": CORPORATE,
        "billing_address": billing_address or REGISTERED,
    })
    assert resp.status_code == 201, resp.text
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": "Gizli-Parola-123"})
    return email, {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _order(
    client: AsyncClient, headers: dict, variant: ProductVariant, address: dict,
    quantity: int = 1, **extra,
):
    return await client.post("/api/v1/orders/checkout", headers=headers, json={
        **CONSENTS,
        "payment_method": "BANK_TRANSFER",
        "items": [{"variant_id": str(variant.id), "quantity": quantity}],
        "shipping_method": "YURTICI_DOOR",
        "shipping_consent": True,
        "shipping_address": address,
        **extra,
    })


# ── The shop's own İstanbul run, and what each method asks for ───────────────


async def test_the_istanbul_run_is_offered_where_the_van_goes(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """Free, and only to a firm whose registered address is in İstanbul.

    Two units reach the ₺20,000 the van asks for; the limit itself, and what
    happens below it, are in ``test_b2b_checkout``.
    """
    _, istanbul = await _registered_firm(client, billing_address={
        **REGISTERED, "city": "istanbul", "district": "Beşiktaş",
    })
    variant = await _variant(db_session, segments)

    quote = await client.post("/api/v1/orders/calculate-cart", headers=istanbul, json={
        "payment_method": "CREDIT_CARD",
        "items": [{"variant_id": str(variant.id), "quantity": 2}],
        "shipping_method": "ISTANBUL_FREE",
    })
    assert quote.status_code == 200, quote.text
    assert quote.json()["shipping"]["reason"] == "free"
    assert (quote.json()["shipping"]["free"], quote.json()["shipping"]["receiver_pays"]) == (True, False)

    # Free means nothing to consent to.
    placed = await _order(
        client, istanbul, variant,
        {**EDITED, "city": "İstanbul", "district": "Beşiktaş"},
        shipping_method="ISTANBUL_FREE", shipping_consent=False, quantity=2,
    )
    assert placed.status_code == 201, placed.text
    assert placed.json()["shipping_payer"] == "SHOP"


async def test_a_firm_outside_istanbul_is_not_offered_the_van(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """Read off the registered address, not off the box typed at checkout."""
    _, antalya = await _registered_firm(client)  # REGISTERED is in Antalya
    variant = await _variant(db_session, segments)
    refused = await client.post("/api/v1/orders/calculate-cart", headers=antalya, json={
        "payment_method": "CREDIT_CARD",
        "items": [{"variant_id": str(variant.id), "quantity": 1}],
        "shipping_method": "ISTANBUL_FREE",
    })
    assert refused.status_code == 422, refused.text
    assert "İstanbul" in refused.json()["detail"]


async def test_a_firm_can_bill_the_carriage_to_its_own_yurtici_agreement(
    client: AsyncClient, db_session: AsyncSession, segments
):
    _, headers = await _registered_firm(client)
    variant = await _variant(db_session, segments)
    placed = await _order(
        client, headers, variant, EDITED, shipping_method="YURTICI_DOOR",
        shipping_contract_no="  YK-99887  ",
    )
    assert placed.status_code == 201, placed.text
    assert placed.json()["shipping_contract_no"] == "YK-99887"


async def test_a_firm_can_name_the_ambar_to_hand_the_goods_to(
    client: AsyncClient, db_session: AsyncSession, segments, admin_user: User
):
    _, headers = await _registered_firm(client)
    variant = await _variant(db_session, segments)
    placed = await _order(
        client, headers, variant, EDITED, shipping_method="NAKLIYE_AMBARI",
        freight_company_name="Aras  Nakliyat Ambarı", freight_phone="0212 000 00 00",
    )
    assert placed.status_code == 201, placed.text
    assert placed.json()["freight_company_name"] == "Aras Nakliyat Ambarı"
    assert placed.json()["freight_phone"] == "0212 000 00 00"

    # The shop sees where to send it.
    sheet = await client.get("/api/v1/admin/orders", headers=auth_header(admin_user))
    row = next(o for o in sheet.json() if o["id"] == placed.json()["id"])
    assert (row["freight_company_name"], row["freight_phone"]) == ("Aras Nakliyat Ambarı", "0212 000 00 00")


@pytest.mark.parametrize(
    ("method", "extra", "kept", "dropped"),
    [
        pytest.param(
            "NAKLIYE_AMBARI",
            {"shipping_contract_no": "YK-1"},
            "freight_company_name",
            "shipping_contract_no",
            id="an agreement number does not belong to an ambar order",
        ),
        pytest.param(
            "YURTICI_DOOR",
            {"freight_company_name": "Aras Ambar", "freight_phone": "0212 000 00 00"},
            "shipping_contract_no",
            "freight_company_name",
            id="an ambar does not belong to a door delivery",
        ),
    ],
)
async def test_each_method_keeps_only_its_own_details(
    client: AsyncClient, db_session: AsyncSession, segments,
    method: str, extra: dict, kept: str, dropped: str,
):
    """Changing the option after typing is not an error — just forgotten."""
    _, headers = await _registered_firm(client)
    variant = await _variant(db_session, segments)
    placed = await _order(client, headers, variant, EDITED, shipping_method=method, **extra)
    assert placed.status_code == 201, placed.text
    assert placed.json()[dropped] is None
    assert kept in placed.json()


async def test_an_ambar_needs_a_number_somebody_answers(
    client: AsyncClient, db_session: AsyncSession, segments
):
    _, headers = await _registered_firm(client)
    variant = await _variant(db_session, segments)
    refused = await _order(
        client, headers, variant, EDITED, shipping_method="NAKLIYE_AMBARI",
        freight_company_name="Aras Ambar", freight_phone="123",
    )
    assert refused.status_code == 422, refused.text


# ── Where a firm may be delivered ────────────────────────────────────────────

async def test_a_firm_may_correct_its_registered_address_for_one_order(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """What "editable" means: the same il and ilçe, the details rewritten."""
    _, headers = await _registered_firm(client)
    variant = await _variant(db_session, segments)
    placed = await _order(client, headers, variant, EDITED)
    assert placed.status_code == 201, placed.text
    # The correction is what ships, and it changed nothing on file.
    assert placed.json()["shipping_address"]["line"] == EDITED["line"]
    saved = await client.get("/api/v1/me/addresses", headers=headers)
    assert [a["line"] for a in saved.json()] == ["Kaleiçi Mah., Atatürk Cad., No: 12"]


async def test_a_firm_cannot_ship_to_an_address_it_never_registered(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """A second branch in another ilçe is a new address, not an edit."""
    _, headers = await _registered_firm(client)
    variant = await _variant(db_session, segments)
    refused = await _order(client, headers, variant, ELSEWHERE_ADDRESS)
    assert refused.status_code == 422, refused.text
    assert "kayıtlı fatura adresinizin" in refused.json()["detail"]


async def test_an_export_customer_is_held_to_its_own_city(
    client: AsyncClient, db_session: AsyncSession, segments
):
    email = f"ship-i-{uuid.uuid4().hex[:8]}@test.com"
    registered = await client.post("/api/v1/auth/register", json={
        "account_type": "INTERNATIONAL",
        "international_kind": "WHOLESALER",
        "email": email,
        "password": "Str0ng-Passw0rd",
        "contact_name": "John Smith",
        "phone": "+49 30 1234 5678",
        "company": {"title": "Smith Trading GmbH", "tax_number": "DE811907980"},
        "billing_address": {"country": "DE", "city": "Berlin", "address_line1": "Hauptstraße 1"},
    })
    assert registered.status_code == 201, registered.text
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": "Str0ng-Passw0rd"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    variant = await _variant(db_session, segments)

    edited = {**ABROAD, "line": "Hauptstraße 1, 4th floor"}
    placed = await _order(client, headers, variant, edited, shipping_method="INTERNATIONAL_FREIGHT")
    assert placed.status_code == 201, placed.text

    refused = await _order(
        client, headers, variant, {**ABROAD, "city": "Hamburg"}, shipping_method="INTERNATIONAL_FREIGHT"
    )
    assert refused.status_code == 422
    assert "registered invoice address" in refused.json()["detail"]


async def test_a_bireysel_customer_ships_wherever_they_like(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """The restriction is a B2B one: a person sends presents."""
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    placed = await _order(client, auth_header(user), variant, ELSEWHERE_ADDRESS)
    assert placed.status_code == 201, placed.text


async def test_a_firm_with_nothing_on_file_can_still_order(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """An account from before the address was collected has none to be held to."""
    user = await _buyer(db_session, segments, AccountType.CAFE, SegmentType.CAFE)
    variant = await _variant(db_session, segments)
    placed = await _order(client, auth_header(user), variant, ELSEWHERE_ADDRESS)
    assert placed.status_code == 201, placed.text


# ── The addresses offered at checkout ────────────────────────────────────────


async def test_checkout_offers_the_addresses_sign_up_collected(client: AsyncClient):
    email = f"addr-{uuid.uuid4().hex[:8]}@test.com"
    registered = await client.post("/api/v1/auth/register", json={
        "account_type": "INDIVIDUAL",
        "email": email,
        "password": "Gizli-Parola-123",
        "full_name": "Ayşe Yılmaz",
        "phone": "0532 111 22 33",
        "billing_address": {
            "neighbourhood": "Kaleiçi Mah.", "street": "Atatürk Cad.", "building_no": "12",
            "door_no": "3", "district": "Muratpaşa", "city": "antalya",
        },
        "company": {
            "entity_type": "COMPANY", "title": "Yılmaz Ltd.", "tax_plate_name": "Yılmaz Ltd.",
            "tax_office": "Muratpaşa", "tax_number": "1234567890",
            "address": {"street": "Bağdat Cad.", "building_no": "400", "district": "Kadıköy", "city": "İstanbul"},
        },
    })
    assert registered.status_code == 201, registered.text
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": "Gizli-Parola-123"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = await client.get("/api/v1/me/addresses", headers=headers)
    assert resp.status_code == 200, resp.text
    by_kind = {a["kind"]: a for a in resp.json()}
    assert set(by_kind) == {"BILLING", "CORPORATE_BILLING"}

    own = by_kind["BILLING"]
    assert (own["label"], own["city"], own["district"]) == ("Fatura Adresi", "Antalya", "Muratpaşa")
    # The structured parts become the one line a waybill carries.
    assert own["line"] == "Kaleiçi Mah., Atatürk Cad., No: 12, Kapı No: 3"
    assert (own["full_name"], own["phone"]) == ("Ayşe Yılmaz", "0532 111 22 33")
    # The firm's address is addressed to the firm.
    assert by_kind["CORPORATE_BILLING"]["full_name"] == "Yılmaz Ltd."


async def test_a_firms_own_address_is_called_its_corporate_one(client: AsyncClient):
    """A firm's invoice address *is* its corporate address: calling the card
    "Fatura Adresi" would read as a personal address beside it."""
    _, headers = await _registered_firm(client)
    resp = await client.get("/api/v1/me/addresses", headers=headers)
    assert resp.status_code == 200, resp.text
    assert [a["label"] for a in resp.json()] == ["Kurumsal Fatura Adresi"]


async def test_shipping_to_a_saved_address_does_not_change_what_is_on_file(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """An order freezes its own copy; nothing is written back, and no second
    address row appears."""
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(user),
        json=_body(variant, shipping_address=ADDRESS, shipping_consent=True),
    )
    assert placed.status_code == 201, placed.text
    resp = await client.get("/api/v1/me/addresses", headers=auth_header(user))
    assert resp.json() == []


async def test_saved_addresses_need_a_login(client: AsyncClient):
    assert (await client.get("/api/v1/me/addresses")).status_code == 401
