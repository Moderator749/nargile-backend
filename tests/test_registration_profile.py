"""Registration for the Turkish account types: bireysel, kafe, tobacco shop.

What each kind has to give, what it may leave out, where the answers end up,
and the two things registration can never do: grant itself a pricing segment,
or take a field that belongs to another kind of account. Nothing here is about
delivery — that is checkout's to ask, and `test_operations` covers it.

A business card says what the customer sells; whether the firm is a şahıs
firması or a şirket is its own answer, given in the invoice section, and it is
that answer — not the card — that decides the tax number's length and whether a
TC number belongs with it.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.b2b import B2BApplication
from app.models.catalog import Product, ProductVariant
from app.models.order import Order, OrderItem, OrderStatus, PaymentMethod
from app.models.user import (
    AddressKind,
    CustomerAddress,
    CustomerProfile,
    CustomerSegment,
    SegmentType,
    User,
)
from tests.conftest import auth_header

# A real TC number has a checksum; this one satisfies it.
TCKN = "10000000146"
VKN = "1234567890"
ADDRESS = {
    "neighbourhood": "Kaleiçi Mah.",
    "street": "Atatürk Cad.",
    "building_no": "12",
    "building_name": "Demir Apt.",
    "door_no": "3",
    "district": "Muratpaşa",
    "city": "antalya",  # canonicalised on the way in
}
OTHER_ADDRESS = {"street": "Bağdat Cad.", "building_no": "400", "district": "Kadıköy", "city": "İstanbul"}
COMPANY = {
    "entity_type": "COMPANY",
    "title": "Demir Nargile Ltd. Şti.",
    "tax_plate_name": "Demir Nargile Ltd. Şti.",
    "tax_office": "Beşiktaş",
    "tax_number": VKN,
}
SOLE_TRADER = {
    "entity_type": "SOLE_PROPRIETOR",
    "title": "Ali Veli Nargile",
    "tax_plate_name": "Ali Veli",
    "tax_office": "Muratpaşa",
    "tax_number": VKN,
}


def _email() -> str:
    return f"kayit-{uuid.uuid4().hex[:8]}@test.com"


def individual(**extra) -> dict:
    return {
        "email": _email(),
        "password": "Gizli-Parola-123",
        "full_name": "Ayşe Yılmaz",
        "phone": "0532 111 22 33",
        **extra,
    }


def business(account_type: str = "CAFE", **extra) -> dict:
    body = {
        "account_type": account_type,
        "email": _email(),
        "password": "Gizli-Parola-123",
        "contact_name": "Mehmet Demir",
        "phone": "0532 444 55 66",
        "company": COMPANY,
        "billing_address": ADDRESS,
        **extra,
    }
    return body


async def register(client: AsyncClient, body: dict) -> dict:
    resp = await client.post("/api/v1/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def refused(client: AsyncClient, body: dict) -> str:
    resp = await client.post("/api/v1/auth/register", json=body)
    assert resp.status_code == 422, resp.text
    return resp.text


async def profile_of(db: AsyncSession, user_id: str) -> CustomerProfile:
    row = await db.scalar(select(CustomerProfile).where(CustomerProfile.user_id == uuid.UUID(user_id)))
    assert row is not None, "every self-registered account gets a profile row"
    return row


async def addresses_of(db: AsyncSession, user_id: str) -> dict[str, CustomerAddress]:
    rows = (
        await db.execute(select(CustomerAddress).where(CustomerAddress.user_id == uuid.UUID(user_id)))
    ).scalars().all()
    return {str(row.kind.value if hasattr(row.kind, "value") else row.kind): row for row in rows}


async def segment_of(db: AsyncSession, user_id: str) -> SegmentType:
    return await db.scalar(
        select(CustomerSegment.code)
        .join(User, User.segment_id == CustomerSegment.id)
        .where(User.id == uuid.UUID(user_id))
    )


# ── Bireysel ─────────────────────────────────────────────────────────────────


async def test_individual_needs_only_a_name_a_mobile_and_an_email(client: AsyncClient, db_session: AsyncSession):
    created = await register(client, individual())
    assert created["account_type"] == "INDIVIDUAL"
    profile = await profile_of(db_session, created["id"])
    assert (profile.national_id, profile.company_title, profile.contact_name) == (None, None, None)
    assert await addresses_of(db_session, created["id"]) == {}
    assert await segment_of(db_session, created["id"]) == SegmentType.RETAIL


async def test_individual_keeps_the_tc_number_and_every_part_of_the_address(
    client: AsyncClient, db_session: AsyncSession
):
    created = await register(client, individual(national_id=TCKN, billing_address=ADDRESS))
    profile = await profile_of(db_session, created["id"])
    assert profile.national_id == TCKN
    billing = (await addresses_of(db_session, created["id"]))["BILLING"]
    assert (billing.neighbourhood, billing.street, billing.building_no, billing.building_name, billing.door_no) == (
        "Kaleiçi Mah.", "Atatürk Cad.", "12", "Demir Apt.", "3",
    )
    assert (billing.district, billing.city) == ("Muratpaşa", "Antalya")  # province canonicalised


# The firm an individual asks to be invoiced instead: its own identity, and its
# own address, which is where the invoice goes.
CORPORATE = {
    "entity_type": "SOLE_PROPRIETOR",
    "title": "Yılmaz Kuruyemiş",
    "tax_plate_name": "Yılmaz Kuruyemiş",
    "tax_office": "Muratpaşa",
    "tax_number": VKN,
    "address": OTHER_ADDRESS,
}


async def test_an_individual_can_ask_for_the_invoice_in_a_firms_name(
    client: AsyncClient, db_session: AsyncSession
):
    created = await register(client, individual(billing_address=ADDRESS, company=CORPORATE))
    profile = await profile_of(db_session, created["id"])
    assert profile.billing_entity_type == "SOLE_PROPRIETOR"
    assert (profile.company_title, profile.tax_office, profile.tax_number) == ("Yılmaz Kuruyemiş", "Muratpaşa", VKN)
    # The name on the firm's tax plate, the same field a firm fills in for itself.
    assert profile.tax_plate_name == "Yılmaz Kuruyemiş"
    # Their own address and the firm's are both kept, each as its own kind.
    rows = await addresses_of(db_session, created["id"])
    assert (rows["BILLING"].city, rows["CORPORATE_BILLING"].city) == ("Antalya", "İstanbul")
    # An invoice is not a price list: the account is still retail, and nothing
    # went to the admin queue.
    assert await segment_of(db_session, created["id"]) == SegmentType.RETAIL
    assert await db_session.scalar(
        select(B2BApplication.id).where(B2BApplication.user_id == uuid.UUID(created["id"]))
    ) is None


@pytest.mark.parametrize(
    "missing",
    [
        pytest.param("entity_type", id="which kind of firm"),
        pytest.param("address", id="where the firm is"),
        pytest.param("tax_plate_name", id="the name on its tax plate"),
        pytest.param("tax_office", id="its vergi dairesi"),
    ],
)
async def test_a_corporate_invoice_is_all_or_nothing(client: AsyncClient, missing: str):
    """It is going on an invoice, so it is asked for in full."""
    await refused(client, individual(
        billing_address=ADDRESS, company={k: v for k, v in CORPORATE.items() if k != missing}
    ))


async def test_a_firms_own_address_is_its_billing_address(client: AsyncClient):
    """A business account has one address, not two: the firm is the account."""
    await refused(client, business(company={**COMPANY, "address": OTHER_ADDRESS}))


@pytest.mark.parametrize("national_id", ["12345678901", "1000000014", "abcdefghijk"])
async def test_a_tc_number_that_fails_its_checksum_is_refused(client: AsyncClient, national_id: str):
    await refused(client, individual(national_id=national_id))


# ── Kafe & Restorant / Nargile & Tobacco Shop ────────────────────────────────


@pytest.mark.parametrize("account_type", ["CAFE", "TOBACCO_SHOP"])
async def test_a_firm_registers_under_what_it_sells(
    client: AsyncClient, db_session: AsyncSession, account_type: str
):
    created = await register(client, business(account_type, landline_phone="0242 123 45 67"))
    assert created["account_type"] == account_type
    # Admin screens show a person; for a firm that is whoever answers the phone.
    assert created["full_name"] == "Mehmet Demir"
    profile = await profile_of(db_session, created["id"])
    assert profile.billing_entity_type == "COMPANY"
    assert (profile.company_title, profile.tax_plate_name, profile.tax_office) == (
        COMPANY["title"], COMPANY["tax_plate_name"], "Beşiktaş",
    )
    assert profile.contact_name == "Mehmet Demir"
    assert (await addresses_of(db_session, created["id"]))["BILLING"].city == "Antalya"


async def test_a_firm_may_be_a_sole_proprietorship_whatever_it_sells(
    client: AsyncClient, db_session: AsyncSession
):
    """The card is the line of business; şahıs firması is the answer below it."""
    created = await register(client, business("TOBACCO_SHOP", company=SOLE_TRADER, national_id=TCKN))
    profile = await profile_of(db_session, created["id"])
    assert profile.billing_entity_type == "SOLE_PROPRIETOR"
    assert (profile.company_title, profile.tax_plate_name) == ("Ali Veli Nargile", "Ali Veli")
    # A şahıs firması is a person, and may say who.
    assert profile.national_id == TCKN


async def test_a_sole_proprietor_may_be_registered_under_their_own_tc_number(
    client: AsyncClient, db_session: AsyncSession
):
    created = await register(client, business(company={**SOLE_TRADER, "tax_number": TCKN}))
    assert (await profile_of(db_session, created["id"])).tax_number == TCKN


async def test_a_company_must_have_a_ten_digit_vkn(client: AsyncClient):
    await refused(client, business(company={**COMPANY, "tax_number": TCKN}))


async def test_a_company_has_no_tc_number_of_its_own(client: AsyncClient):
    await refused(client, business(company=COMPANY, national_id=TCKN))


async def test_a_firm_has_to_say_which_kind_of_firm_it_is(client: AsyncClient):
    await refused(client, business(company={k: v for k, v in COMPANY.items() if k != "entity_type"}))


@pytest.mark.parametrize("account_type", ["SOLE_PROPRIETOR", "COMPANY"])
async def test_the_cards_the_form_no_longer_offers_are_refused(client: AsyncClient, account_type: str):
    """They stay in the enum for rows written before the cards changed, not for
    new sign-ups."""
    await refused(client, business(account_type))


@pytest.mark.parametrize("account_type", ["INDIVIDUAL", "CAFE", "TOBACCO_SHOP"])
async def test_no_sign_up_asks_for_a_price_group_or_is_granted_one(
    client: AsyncClient, db_session: AsyncSession, account_type: str
):
    """Nobody is asked which group they would like, and nobody is given one:
    every self-service account trades at retail until an admin says otherwise.
    A payload that asks anyway is ignored."""
    body = individual() if account_type == "INDIVIDUAL" else business(account_type)
    created = await register(client, {**body, "requested_segment": "DEALER"})
    assert await segment_of(db_session, created["id"]) == SegmentType.RETAIL


@pytest.mark.parametrize(
    ("account_type", "suggested"),
    [
        pytest.param("CAFE", SegmentType.CAFE, id="a kafe is suggested the kafe list"),
        pytest.param("TOBACCO_SHOP", SegmentType.DEALER, id="a shop resells, so bayi"),
    ],
)
async def test_a_firm_puts_itself_in_the_approval_queue_by_registering(
    client: AsyncClient, db_session: AsyncSession, account_type: str, suggested: SegmentType
):
    """A firm that registered and was never looked at would sit at retail prices
    with nobody aware of it, so sign-up opens the application itself."""
    created = await register(client, business(account_type))
    application = await db_session.scalar(
        select(B2BApplication).where(B2BApplication.user_id == uuid.UUID(created["id"]))
    )
    assert application is not None, "a business sign-up should be waiting for an admin"
    assert application.status == "PENDING"
    # A suggestion read off what they sell, not a grant: still retail.
    assert application.requested_segment == suggested
    assert await segment_of(db_session, created["id"]) == SegmentType.RETAIL
    # Carried over from the form, so nothing is typed twice.
    assert (application.company_title, application.tax_number, application.tax_office) == (
        COMPANY["title"], VKN, COMPANY["tax_office"],
    )


async def test_a_bireysel_sign_up_opens_no_application(
    client: AsyncClient, db_session: AsyncSession
):
    created = await register(client, individual())
    assert await db_session.scalar(
        select(B2BApplication.id).where(B2BApplication.user_id == uuid.UUID(created["id"]))
    ) is None


async def test_the_admin_queue_shows_a_firm_the_moment_it_registers(
    client: AsyncClient, admin_user: User
):
    created = await register(client, business("TOBACCO_SHOP"))
    pending = await client.get("/api/v1/admin/b2b-applications", headers=auth_header(admin_user))
    assert pending.status_code == 200, pending.text
    waiting = next(a for a in pending.json() if a["user_id"] == created["id"])
    assert (waiting["status"], waiting["requested_segment"]) == ("PENDING", "DEALER")
    assert waiting["company_title"] == COMPANY["title"]


async def test_approving_what_a_sign_up_opened_grants_the_price_list(
    client: AsyncClient, db_session: AsyncSession, admin_user: User
):
    """The whole point of the queue: one approval and the firm is on its list."""
    created = await register(client, business("CAFE"))
    application = await db_session.scalar(
        select(B2BApplication).where(B2BApplication.user_id == uuid.UUID(created["id"]))
    )
    approved = await client.post(
        f"/api/v1/admin/b2b-applications/{application.id}/approve",
        headers=auth_header(admin_user),
        json={},
    )
    assert approved.status_code == 200, approved.text
    assert await segment_of(db_session, created["id"]) == SegmentType.CAFE


@pytest.mark.parametrize(
    "missing",
    [
        pytest.param({"billing_address": None}, id="no billing address"),
        pytest.param({"company": None}, id="no company"),
        pytest.param({"contact_name": None}, id="no contact person"),
        pytest.param({"company": {k: v for k, v in COMPANY.items() if k != "tax_plate_name"}}, id="no tax plate name"),
        pytest.param({"company": {k: v for k, v in COMPANY.items() if k != "tax_office"}}, id="no tax office"),
    ],
)
async def test_a_business_account_needs_its_whole_invoice_identity(client: AsyncClient, missing: dict):
    await refused(client, business(**missing))


async def test_a_signup_takes_no_delivery_data(client: AsyncClient, db_session: AsyncSession):
    """Delivery left for checkout, where there is an order to route. A client
    still sending a preference is not an error — the account is created and the
    block ignored, because nothing on the form can produce one any more."""
    created = await register(client, individual(
        billing_address=ADDRESS,
        delivery={
            "mode": "DIFFERENT_ADDRESS",
            "address": OTHER_ADDRESS,
            "receiver_name": "Komşu Ali",
            "receiver_phone": "0533 222 11 00",
        },
    ))
    # Only the invoice address was kept, and nowhere does a preference survive.
    assert set(await addresses_of(db_session, created["id"])) == {"BILLING"}
    assert not any(c.name.startswith(("delivery", "receiver")) for c in CustomerProfile.__table__.c)


# ── Across all of them ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address",
    [
        pytest.param({**ADDRESS, "city": "Narnia"}, id="province is not one of the 81"),
        pytest.param({**ADDRESS, "neighbourhood": None, "street": None}, id="neither a neighbourhood nor a street"),
        pytest.param({**ADDRESS, "district": ""}, id="no district"),
    ],
)
async def test_an_address_nobody_could_deliver_to_is_refused(client: AsyncClient, address: dict):
    await refused(client, individual(billing_address=address))


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param({"password": "kisa"}, id="password under eight characters"),
        pytest.param({"email": "bu-bir-eposta-degil"}, id="not an e-mail address"),
        pytest.param({"phone": None}, id="no mobile number"),
        pytest.param({"phone": "123"}, id="mobile number too short"),
        pytest.param({"full_name": " "}, id="no name"),
        pytest.param({"username": "a!"}, id="username with punctuation"),
    ],
)
async def test_the_login_itself_is_checked(client: AsyncClient, broken: dict):
    await refused(client, individual(**broken))


async def test_a_username_is_a_second_way_to_log_in(client: AsyncClient):
    body = individual(username="Ayse.Yilmaz")
    await register(client, body)
    for given in (body["email"].upper(), "ayse.yilmaz", "Ayse.Yilmaz"):
        resp = await client.post("/api/v1/auth/login", json={"email": given, "password": body["password"]})
        assert resp.status_code == 200, f"{given} should be able to log in: {resp.text}"


@pytest.mark.parametrize(
    "second,detail",
    [
        pytest.param("username", "Username already taken", id="username, in another case"),
        pytest.param("email", "Email already registered", id="e-mail, in another case"),
    ],
)
async def test_neither_the_email_nor_the_username_can_be_taken_twice(
    client: AsyncClient, second: str, detail: str
):
    taken = f"Tekil.{uuid.uuid4().hex[:6]}"
    first = individual(username=taken)
    await register(client, first)
    clash = (
        individual(username=taken.lower())
        if second == "username"
        else individual(email=first["email"].upper())
    )
    resp = await client.post("/api/v1/auth/register", json=clash)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == detail


async def test_the_order_sheet_invoices_the_firm_at_its_billing_address(
    client: AsyncClient, db_session: AsyncSession, admin_user: User
):
    """What all of this is for: an order that can be invoiced without asking."""
    created = await register(client, business())
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"FTR-{suffix}", name=f"Fatura Testi {suffix}", tags=[])
    db_session.add(product)
    await db_session.flush()
    variant = ProductVariant(product_id=product.id, sku=f"FTR-{suffix}")
    db_session.add(variant)
    await db_session.flush()
    db_session.add(Order(
        order_number=f"FTR-{suffix.upper()}",
        user_id=uuid.UUID(created["id"]),
        total_amount=Decimal("1000.00"),
        payment_method=PaymentMethod.BANK_TRANSFER,
        status=OrderStatus.PENDING_APPROVAL,
        items=[OrderItem(variant_id=variant.id, quantity=1, unit_price=Decimal("1000.00"), line_total=Decimal("1000.00"))],
    ))
    await db_session.commit()

    orders = await client.get("/api/v1/admin/orders", headers=auth_header(admin_user))
    assert orders.status_code == 200, orders.text
    sheet = next(o for o in orders.json() if o["order_number"] == f"FTR-{suffix.upper()}")
    assert sheet["company_title"] == COMPANY["title"]
    assert sheet["billing_address"] == {
        "full_name": COMPANY["title"],
        "phone": "0532 444 55 66",
        "city": "Antalya",
        "district": "Muratpaşa",
        "line": "Kaleiçi Mah., Atatürk Cad., No: 12, Demir Apt., Kapı No: 3",
    }


# ── Ticari elektronik ileti onayı ────────────────────────────────────────────


async def _login_headers(client: AsyncClient, body: dict) -> dict:
    resp = await client.post("/api/v1/auth/login", json={"email": body["email"], "password": body["password"]})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_registration_without_the_consent_box_records_no_consent(client: AsyncClient, db_session: AsyncSession):
    """Unticked — and not sent at all, as an older client would — leaves both
    channels NULL. Consent is never assumed, and registration still succeeds."""
    body = individual()
    created = await register(client, body)
    user = await db_session.get(User, uuid.UUID(created["id"]))
    assert user.marketing_email_consent_at is None
    assert user.marketing_sms_consent_at is None
    prefs = await client.get("/api/v1/me/communication-preferences", headers=await _login_headers(client, body))
    assert prefs.json() == {"email": False, "sms": False}

    created = await register(client, individual(marketing_consent=False))
    user = await db_session.get(User, uuid.UUID(created["id"]))
    assert user.marketing_email_consent_at is None and user.marketing_sms_consent_at is None


@pytest.mark.asyncio
async def test_registration_with_the_consent_box_stamps_both_channels(client: AsyncClient, db_session: AsyncSession):
    """Ticked: e-mail and SMS consent carry the server's time of registration,
    and the account page shows both channels on."""
    body = individual(marketing_consent=True)
    created = await register(client, body)
    user = await db_session.get(User, uuid.UUID(created["id"]))
    assert user.marketing_email_consent_at is not None
    assert user.marketing_sms_consent_at == user.marketing_email_consent_at
    assert abs((user.marketing_email_consent_at - user.created_at).total_seconds()) < 5
    prefs = await client.get("/api/v1/me/communication-preferences", headers=await _login_headers(client, body))
    assert prefs.json() == {"email": True, "sms": True}


@pytest.mark.asyncio
async def test_consent_box_is_accepted_for_business_and_international_accounts(client: AsyncClient, db_session: AsyncSession):
    """The box is the same on every card; a firm's contact consents for the firm."""
    created = await register(client, business(marketing_consent=True))
    user = await db_session.get(User, uuid.UUID(created["id"]))
    assert user.marketing_email_consent_at is not None and user.marketing_sms_consent_at is not None

