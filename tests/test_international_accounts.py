"""Registration from outside Turkey.

The fourth account type is held to none of the Turkish rules — no TC checksum,
no VKN length, no province from the list of 81, no mahalle — and to a few of
its own: a real country, an address line, and a word on what it buys for. Both
answers to that are businesses; there is no private export customer. Like every
other sign-up it is asked for no price group — but, like every other firm, it
opens an application for one, and what it buys for is the group that
application suggests. Freight is checkout's question anyway.
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
from app.models.user import CustomerAddress, CustomerProfile, SegmentType, User
from tests.conftest import auth_header
from tests.test_registration_profile import (
    ADDRESS as TR_ADDRESS,
    TCKN,
    addresses_of,
    individual,
    profile_of,
    refused,
    register,
    segment_of,
)

GERMANY = {
    "country": "de",  # canonicalised on the way in
    "city": "Berlin",
    "postal_code": "10115",
    "address_line1": "Hauptstraße 1",
    "address_line2": "Apt 5",
}


def international(kind: str = "WHOLESALER", **extra) -> dict:
    return {
        "account_type": "INTERNATIONAL",
        "international_kind": kind,
        "email": f"intl-{uuid.uuid4().hex[:8]}@test.com",
        "password": "Str0ng-Passw0rd",
        "phone": "+49 30 1234 5678",
        "contact_name": "John Smith",
        "company": {"title": "Smith Trading GmbH", "tax_number": "DE811907980"},
        "billing_address": GERMANY,
        **extra,
    }


# ── What it stores ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["WHOLESALER", "CAFE"])
async def test_an_international_business_registers_with_its_vat_number(
    client: AsyncClient, db_session: AsyncSession, kind: str
):
    created = await register(client, international(kind))
    assert created["account_type"] == "INTERNATIONAL"
    # Admin screens show a person; for a firm that is its contact.
    assert created["full_name"] == "John Smith"
    profile = await profile_of(db_session, created["id"])
    assert profile.company_title == "Smith Trading GmbH"
    # Kept exactly as typed: no digits-only, no checksum, no length.
    assert profile.tax_number == "DE811907980"
    assert profile.billing_entity_type == "COMPANY"  # invoiced as an organisation
    assert (profile.contact_name, profile.national_id, profile.tax_office) == ("John Smith", None, None)
    # What they buy for is kept: it is what the sales team quotes against.
    assert profile.international_kind == kind
    # Still retail, but waiting on an admin rather than forgotten: what they
    # buy for becomes the group their application suggests.
    assert await segment_of(db_session, created["id"]) == SegmentType.RETAIL
    application = await db_session.scalar(
        select(B2BApplication).where(B2BApplication.user_id == uuid.UUID(created["id"]))
    )
    assert application.status == "PENDING"
    assert application.requested_segment == (
        SegmentType.WHOLESALE if kind == "WHOLESALER" else SegmentType.CAFE
    )


async def test_the_address_is_stored_in_its_international_shape(
    client: AsyncClient, db_session: AsyncSession
):
    created = await register(client, international())
    billing = (await addresses_of(db_session, created["id"]))["BILLING"]
    assert (billing.country, billing.city, billing.postal_code) == ("DE", "Berlin", "10115")
    assert (billing.address_line1, billing.address_line2) == ("Hauptstraße 1", "Apt 5")
    # None of the Turkish columns were filled in on the way past.
    assert (billing.neighbourhood, billing.street, billing.building_no, billing.district) == (
        None, None, None, None,
    )


@pytest.mark.parametrize("kind", ["INDIVIDUAL", "BUSINESS", "RETAIL", ""])
async def test_only_the_two_kinds_the_form_offers_are_accepted(client: AsyncClient, kind: str):
    """"Business or private" is the question it used to ask; both answers to the
    question it asks now are businesses."""
    await refused(client, international(kind))


@pytest.mark.parametrize(
    "tax_number",
    ["GB123456789012", "FR 12 345 678 901", "12-3456789", None],
    ids=["a 14-character VAT number", "spaces and all", "a US EIN", "none at all"],
)
async def test_a_tax_number_is_optional_and_in_whatever_shape_its_country_uses(
    client: AsyncClient, db_session: AsyncSession, tax_number: str | None
):
    created = await register(client, international(
        company={"title": "Smith Trading GmbH", "tax_number": tax_number}
    ))
    assert (await profile_of(db_session, created["id"])).tax_number == tax_number
    # The application opens either way: a firm with no VAT number still waits.
    application = await db_session.scalar(
        select(B2BApplication).where(B2BApplication.user_id == uuid.UUID(created["id"]))
    )
    assert application.tax_number == tax_number


@pytest.mark.parametrize(
    "phone",
    ["+1 (555) 123-4567", "+49 30 1234 5678", "+677 12345", "0049.30.123456"],
    ids=["US with brackets", "German spacing", "a short Pacific number", "dots and a prefix"],
)
async def test_phone_numbers_are_accepted_the_way_each_country_writes_them(
    client: AsyncClient, phone: str
):
    await register(client, international(phone=phone))


async def test_a_state_or_province_longer_than_a_turkish_province_fits(
    client: AsyncClient, db_session: AsyncSession
):
    """`city` holds a city, a state or a province, so it cannot be 40 characters."""
    state = "Autonomous Region in Muslim Mindanao"
    created = await register(client, international(
        billing_address={**GERMANY, "country": "PH", "city": state}
    ))
    assert (await addresses_of(db_session, created["id"]))["BILLING"].city == state


async def test_a_postal_code_is_optional(client: AsyncClient, db_session: AsyncSession):
    created = await register(client, international(
        billing_address={k: v for k, v in GERMANY.items() if k != "postal_code"}
    ))
    assert (await addresses_of(db_session, created["id"]))["BILLING"].postal_code is None


# ── What it refuses ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "turkish",
    [
        pytest.param({"national_id": TCKN}, id="a TC identity number"),
        pytest.param({"billing_address": TR_ADDRESS}, id="a Turkish address"),
        pytest.param({"billing_address": {**GERMANY, "country": "TR"}}, id="Türkiye as the country"),
        pytest.param(
            {"company": {"title": "Smith Trading GmbH", "tax_number": "DE811907980", "tax_office": "Beşiktaş"}},
            id="a vergi dairesi",
        ),
        pytest.param(
            {"company": {"title": "Smith GmbH", "tax_number": "DE811907980", "tax_plate_name": "John Smith"}},
            id="a tax plate name",
        ),
        pytest.param(
            {"company": {"title": "Smith GmbH", "tax_number": "DE811907980", "address": dict(GERMANY)}},
            id="a second address for the company",
        ),
    ],
)
async def test_nothing_turkish_belongs_on_an_international_account(client: AsyncClient, turkish: dict):
    await refused(client, international(**turkish))


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param({"international_kind": None}, id="no answer on what they buy for"),
        pytest.param({"billing_address": None}, id="no address"),
        pytest.param({"billing_address": {**GERMANY, "address_line1": ""}}, id="no address line"),
        pytest.param({"billing_address": {**GERMANY, "country": "ZZ"}}, id="a country that does not exist"),
        pytest.param({"billing_address": {**GERMANY, "city": ""}}, id="no city"),
        pytest.param({"phone": "12345"}, id="too few digits for any country"),
        pytest.param({"contact_name": None}, id="no contact person"),
        pytest.param({"company": None}, id="no company name"),
    ],
)
async def test_an_international_account_still_needs_the_basics(client: AsyncClient, broken: dict):
    await refused(client, international(**broken))


# ── What the shop sees ───────────────────────────────────────────────────────


async def test_the_order_sheet_names_the_country_it_ships_to(
    client: AsyncClient, db_session: AsyncSession, admin_user: User
):
    created = await register(client, international())
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"INT-{suffix}", name=f"Export Testi {suffix}", tags=[])
    db_session.add(product)
    await db_session.flush()
    variant = ProductVariant(product_id=product.id, sku=f"INT-{suffix}")
    db_session.add(variant)
    await db_session.flush()
    db_session.add(Order(
        order_number=f"INT-{suffix.upper()}",
        user_id=uuid.UUID(created["id"]),
        total_amount=Decimal("2500.00"),
        payment_method=PaymentMethod.BANK_TRANSFER,
        status=OrderStatus.PENDING_APPROVAL,
        items=[OrderItem(variant_id=variant.id, quantity=1, unit_price=Decimal("2500.00"), line_total=Decimal("2500.00"))],
    ))
    await db_session.commit()

    orders = await client.get("/api/v1/admin/orders", headers=auth_header(admin_user))
    assert orders.status_code == 200, orders.text
    sheet = next(o for o in orders.json() if o["order_number"] == f"INT-{suffix.upper()}")
    assert sheet["company_title"] == "Smith Trading GmbH"
    assert sheet["billing_address"] == {
        "full_name": "Smith Trading GmbH",
        "phone": "+49 30 1234 5678",
        "city": "Berlin",
        "district": None,
        "line": "Hauptstraße 1, Apt 5, 10115",
        "country": "Germany",  # the code is stored, the name is printed
    }


async def test_deleting_an_international_customer_takes_their_address_with_them(
    client: AsyncClient, db_session: AsyncSession
):
    created = await register(client, international())
    user = await db_session.get(User, uuid.UUID(created["id"]))
    await db_session.delete(user)
    await db_session.commit()
    for model in (CustomerProfile, CustomerAddress):
        assert await db_session.scalar(
            select(model.id).where(model.user_id == uuid.UUID(created["id"]))
        ) is None
