"""The admin customers grid, one customer's page, and assigning a price list.

Sign-up grants no price tier, so this is the only way a kafe or a bayi gets
theirs. The tests follow an account from the grid to its page to its segment,
and check that the storefront prices it the moment the segment changes.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice, WarehouseType
from app.models.order import Order, OrderItem, OrderStatus, PaymentMethod
from app.models.user import CustomerSegment, SegmentType, User
from tests.conftest import auth_header

ADDRESS = {
    "neighbourhood": "Kaleiçi Mah.",
    "street": "Atatürk Cad.",
    "building_no": "12",
    "door_no": "3",
    "district": "Muratpaşa",
    "city": "antalya",
}
COMPANY = {
    "entity_type": "COMPANY",
    "title": "Demir Nargile Ltd. Şti.",
    "tax_plate_name": "Demir Nargile Ltd. Şti.",
    "tax_office": "Beşiktaş",
    "tax_number": "1234567890",
}


async def _register(client: AsyncClient, body: dict) -> dict:
    resp = await client.post("/api/v1/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _individual(client: AsyncClient, **extra) -> dict:
    return await _register(client, {
        "account_type": "INDIVIDUAL",
        "email": f"grid-b-{uuid.uuid4().hex[:8]}@test.com",
        "password": "Gizli-Parola-123",
        "full_name": "Ayşe Yılmaz",
        "phone": "0532 111 22 33",
        **extra,
    })


async def _firm(client: AsyncClient, account_type: str = "CAFE", **extra) -> dict:
    return await _register(client, {
        "account_type": account_type,
        "email": f"grid-f-{uuid.uuid4().hex[:8]}@test.com",
        "password": "Gizli-Parola-123",
        "contact_name": "Mehmet Demir",
        "phone": "0532 444 55 66",
        "company": COMPANY,
        "billing_address": ADDRESS,
        **extra,
    })


async def _grid(client: AsyncClient, admin: User, **params) -> tuple[list[dict], int]:
    resp = await client.get("/api/v1/admin/customers", headers=auth_header(admin), params=params)
    assert resp.status_code == 200, resp.text
    return resp.json(), int(resp.headers["X-Total-Count"])


async def _page(client: AsyncClient, admin: User, user_id: str) -> dict:
    resp = await client.get(f"/api/v1/admin/customers/{user_id}", headers=auth_header(admin))
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── The grid ─────────────────────────────────────────────────────────────────


async def test_the_grid_shows_what_each_account_is_and_what_it_bought(
    client: AsyncClient, admin_user: User, db_session: AsyncSession
):
    firm = await _firm(client)
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"GRD-{suffix}", name=f"Grid {suffix}", tags=[])
    db_session.add(product)
    await db_session.flush()
    variant = ProductVariant(product_id=product.id, sku=f"GRD-{suffix}")
    db_session.add(variant)
    await db_session.flush()
    db_session.add(Order(
        order_number=f"GRD-{suffix.upper()}",
        user_id=uuid.UUID(firm["id"]),
        total_amount=Decimal("100.00"),
        payment_method=PaymentMethod.BANK_TRANSFER,
        status=OrderStatus.PENDING_APPROVAL,
        items=[OrderItem(variant_id=variant.id, quantity=1, unit_price=Decimal("100.00"), line_total=Decimal("100.00"))],
    ))
    await db_session.commit()

    rows, _ = await _grid(client, admin_user, q=firm["email"])
    assert len(rows) == 1
    row = rows[0]
    assert row["account_type"] == "CAFE"
    # A firm is known by its unvan, and reachable on its contact's number.
    assert (row["company_title"], row["full_name"]) == (COMPANY["title"], "Mehmet Demir")
    assert row["phone"] == "0532 444 55 66"
    assert row["segment"] == "RETAIL"  # until an admin says otherwise
    assert row["order_count"] == 1
    assert row["created_at"]


async def test_the_grid_says_where_each_customer_is(client: AsyncClient, admin_user: User):
    """A Turkish row carries its şehir and ilçe, a foreign one its country."""
    firm = await _firm(client)
    abroad = await _register(client, {
        "account_type": "INTERNATIONAL",
        "international_kind": "CAFE",
        "email": f"grid-w-{uuid.uuid4().hex[:8]}@test.com",
        "password": "Str0ng-Passw0rd",
        "contact_name": "Maria Rossi",
        "phone": "+39 02 1234 567",
        "company": {"title": "Rossi SRL", "tax_number": "IT12345678901"},
        "billing_address": {"country": "IT", "city": "Lombardia", "address_line1": "Via Roma 7"},
    })

    rows, _ = await _grid(client, admin_user, q=firm["email"])
    assert (rows[0]["country"], rows[0]["city"], rows[0]["district"]) == ("TR", "Antalya", "Muratpaşa")

    rows, _ = await _grid(client, admin_user, q=abroad["email"])
    assert (rows[0]["country"], rows[0]["city"]) == ("IT", "Lombardia")
    assert rows[0]["district"] is None


async def test_a_customer_with_no_address_has_no_place_and_still_lists(
    client: AsyncClient, admin_user: User
):
    created = await _individual(client)  # no billing address: it is optional
    rows, _ = await _grid(client, admin_user, q=created["email"])
    assert len(rows) == 1
    assert (rows[0]["city"], rows[0]["district"], rows[0]["country"]) == (None, None, None)


async def test_the_firms_own_address_is_the_one_shown(client: AsyncClient, admin_user: User):
    """An individual invoiced in a firm's name has two addresses; the grid shows
    their own, and falls back to the firm's when that is all there is."""
    both = await _individual(client, billing_address=ADDRESS, company={
        "entity_type": "COMPANY",
        "title": "Yılmaz Ltd.",
        "tax_plate_name": "Yılmaz Ltd.",
        "tax_office": "Muratpaşa",
        "tax_number": "1234567890",
        "address": {"street": "Bağdat Cad.", "building_no": "400", "district": "Kadıköy", "city": "İstanbul"},
    })
    rows, _ = await _grid(client, admin_user, q=both["email"])
    assert (rows[0]["city"], rows[0]["district"]) == ("Antalya", "Muratpaşa")


async def test_a_firm_can_be_found_by_its_unvan(client: AsyncClient, admin_user: User):
    firm = await _firm(client)
    rows, total = await _grid(client, admin_user, q="Demir Nargile")
    assert total >= 1
    assert firm["email"] in [r["email"] for r in rows]


@pytest.mark.parametrize("account_type", ["INDIVIDUAL", "CAFE", "TOBACCO_SHOP"])
async def test_the_grid_filters_by_account_type(client: AsyncClient, admin_user: User, account_type: str):
    created = await _individual(client) if account_type == "INDIVIDUAL" else await _firm(client, account_type)
    rows, _ = await _grid(client, admin_user, account_type=account_type, limit=200)
    assert created["email"] in [r["email"] for r in rows]
    assert {r["account_type"] for r in rows} == {account_type}


async def test_the_grid_filters_by_price_list(
    client: AsyncClient, admin_user: User, segments: dict[SegmentType, CustomerSegment]
):
    firm = await _firm(client)
    await _assign(client, admin_user, firm["id"], "WHOLESALE")
    rows, _ = await _grid(client, admin_user, segment="WHOLESALE", limit=200)
    assert firm["email"] in [r["email"] for r in rows]
    assert {r["segment"] for r in rows} == {"WHOLESALE"}


async def test_the_newest_account_is_first_unless_asked_otherwise(client: AsyncClient, admin_user: User):
    first = await _individual(client)
    second = await _individual(client)
    rows, _ = await _grid(client, admin_user, q="grid-b-", limit=200)
    emails = [r["email"] for r in rows]
    assert emails.index(second["email"]) < emails.index(first["email"])

    oldest_first, _ = await _grid(client, admin_user, q="grid-b-", order_by="created", order_dir="asc", limit=200)
    assert [r["email"] for r in oldest_first] == emails[::-1]


async def test_the_grid_sorts_by_the_name_it_shows(client: AsyncClient, admin_user: User):
    """A firm sorts under its unvan, a person under their name."""
    await _firm(client, company={**COMPANY, "title": "Zeta Nargile"})
    await _firm(client, company={**COMPANY, "title": "Alfa Nargile"})
    rows, _ = await _grid(client, admin_user, q="Nargile", order_by="name", order_dir="asc", limit=200)
    titles = [r["company_title"] for r in rows if r["company_title"] in ("Alfa Nargile", "Zeta Nargile")]
    assert titles.index("Alfa Nargile") < titles.index("Zeta Nargile")


async def test_the_grid_pages_and_counts_every_match(client: AsyncClient, admin_user: User):
    for _ in range(3):
        await _individual(client)
    page, total = await _grid(client, admin_user, q="grid-b-", limit=2)
    assert len(page) == 2 and total >= 3
    rest, _ = await _grid(client, admin_user, q="grid-b-", skip=2, limit=2)
    assert {r["email"] for r in page}.isdisjoint({r["email"] for r in rest})


# ── One customer's page ──────────────────────────────────────────────────────


async def test_a_firms_page_shows_its_whole_invoice_identity(client: AsyncClient, admin_user: User):
    firm = await _firm(client, "TOBACCO_SHOP", landline_phone="0242 123 45 67")
    page = await _page(client, admin_user, firm["id"])
    assert page["account_type"] == "TOBACCO_SHOP"
    assert (page["phone"], page["landline_phone"]) == ("0532 444 55 66", "0242 123 45 67")
    profile = page["profile"]
    assert profile["billing_entity_type"] == "COMPANY"
    assert (profile["company_title"], profile["tax_plate_name"]) == (COMPANY["title"], COMPANY["tax_plate_name"])
    assert (profile["tax_office"], profile["tax_number"]) == ("Beşiktaş", "1234567890")
    assert profile["contact_name"] == "Mehmet Demir"
    billing = next(a for a in page["addresses"] if a["kind"] == "BILLING")
    assert (billing["country"], billing["city"], billing["district"]) == ("TR", "Antalya", "Muratpaşa")
    assert (billing["neighbourhood"], billing["building_no"], billing["door_no"]) == ("Kaleiçi Mah.", "12", "3")


async def test_an_individuals_page_shows_both_of_their_addresses(client: AsyncClient, admin_user: User):
    created = await _individual(client, billing_address=ADDRESS, national_id="10000000146", company={
        "entity_type": "SOLE_PROPRIETOR",
        "title": "Yılmaz Kuruyemiş",
        "tax_plate_name": "Ayşe Yılmaz",
        "tax_office": "Muratpaşa",
        "tax_number": "1234567890",
        "address": {"street": "Bağdat Cad.", "building_no": "400", "district": "Kadıköy", "city": "İstanbul"},
    })
    page = await _page(client, admin_user, created["id"])
    assert page["profile"]["national_id"] == "10000000146"
    assert page["profile"]["billing_entity_type"] == "SOLE_PROPRIETOR"
    kinds = {a["kind"]: a["city"] for a in page["addresses"]}
    assert kinds == {"BILLING": "Antalya", "CORPORATE_BILLING": "İstanbul"}


async def test_an_international_page_shows_the_address_in_its_own_shape(client: AsyncClient, admin_user: User):
    created = await _register(client, {
        "account_type": "INTERNATIONAL",
        "international_kind": "WHOLESALER",
        "email": f"grid-i-{uuid.uuid4().hex[:8]}@test.com",
        "password": "Str0ng-Passw0rd",
        "contact_name": "John Smith",
        "phone": "+49 30 1234 5678",
        "company": {"title": "Smith Trading GmbH", "tax_number": "DE811907980"},
        "billing_address": {
            "country": "DE", "city": "Berlin", "postal_code": "10115", "address_line1": "Hauptstraße 1",
        },
    })
    page = await _page(client, admin_user, created["id"])
    assert page["profile"]["international_kind"] == "WHOLESALER"
    assert page["profile"]["tax_number"] == "DE811907980"
    address = page["addresses"][0]
    assert (address["country"], address["city"], address["postal_code"]) == ("DE", "Berlin", "10115")
    assert address["address_line1"] == "Hauptstraße 1"
    # Nothing Turkish was invented to fill the gaps.
    assert (address["district"], address["neighbourhood"]) == (None, None)


async def test_a_page_shows_a_standing_application_without_deciding_it(
    client: AsyncClient, admin_user: User, customer_user: User
):
    await client.post("/api/v1/me/b2b-application", headers=auth_header(customer_user), json={
        "segment": "DEALER", "company_title": "Köşe Kafe Ltd.", "tax_number": "1234567890",
    })
    page = await _page(client, admin_user, str(customer_user.id))
    assert (page["application_status"], page["requested_segment"]) == ("PENDING", "DEALER")
    assert page["segment"] == "RETAIL"  # reading it grants nothing


async def test_a_customer_who_does_not_exist_is_a_404(client: AsyncClient, admin_user: User):
    resp = await client.get(f"/api/v1/admin/customers/{uuid.uuid4()}", headers=auth_header(admin_user))
    assert resp.status_code == 404


# ── Assigning a price list ───────────────────────────────────────────────────


async def _assign(client: AsyncClient, admin: User, user_id: str, segment: str) -> dict:
    resp = await client.put(
        f"/api/v1/admin/customers/{user_id}/segment", headers=auth_header(admin), json={"segment": segment}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("segment", ["CAFE", "WHOLESALE", "DEALER", "RETAIL"])
async def test_an_admin_puts_a_customer_on_any_price_list(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, segment: str
):
    firm = await _firm(client)
    assert (await _assign(client, admin_user, firm["id"], segment))["segment"] == segment
    stored = await db_session.scalar(
        select(CustomerSegment.code)
        .join(User, User.segment_id == CustomerSegment.id)
        .where(User.id == uuid.UUID(firm["id"]))
    )
    assert stored == SegmentType(segment)


async def test_a_new_tier_prices_the_storefront_at_once(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, segments: dict[SegmentType, CustomerSegment]
):
    """What the tier is for: the customer's next request is priced on it, with no
    re-login — prices are read from the account, never cached into a token."""
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"SEG-{suffix}", name=f"Segment {suffix}", tags=[])
    db_session.add(product)
    await db_session.flush()
    variant = ProductVariant(product_id=product.id, sku=f"SEG-{suffix}")
    db_session.add(variant)
    await db_session.flush()
    for code, incl in ((SegmentType.RETAIL, "1200.00"), (SegmentType.DEALER, "800.00")):
        db_session.add(VariantPrice(
            variant_id=variant.id, segment_id=segments[code].id, currency="TRY",
            price_excl_tax=(Decimal(incl) / Decimal("1.2")).quantize(Decimal("0.01")),
            price_incl_tax=Decimal(incl), vat_rate=Decimal("20.00"),
        ))
    db_session.add(Inventory(variant_id=variant.id, warehouse_type=WarehouseType.RETAIL_STOCK, quantity=5))
    await db_session.commit()

    email = f"grid-p-{uuid.uuid4().hex[:8]}@test.com"
    created = await _register(client, {
        "account_type": "CAFE", "email": email, "password": "Gizli-Parola-123",
        "contact_name": "Mehmet Demir", "phone": "0532 444 55 66",
        "company": COMPANY, "billing_address": ADDRESS,
    })
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": "Gizli-Parola-123"})
    token = {"Authorization": f"Bearer {login.json()['access_token']}"}

    async def shown_price() -> str:
        resp = await client.get(f"/api/v1/products/{product.id}", headers=token)
        assert resp.status_code == 200, resp.text
        return resp.json()["variants"][0]["price"]["price_incl_tax"]

    assert await shown_price() == "1200.00"  # retail, as every sign-up starts
    await _assign(client, admin_user, created["id"], "DEALER")
    assert await shown_price() == "800.00"  # the same request, the dealer's price


async def test_a_price_list_that_does_not_exist_is_refused(client: AsyncClient, admin_user: User):
    firm = await _firm(client)
    resp = await client.put(
        f"/api/v1/admin/customers/{firm['id']}/segment",
        headers=auth_header(admin_user),
        json={"segment": "PLATINUM"},
    )
    assert resp.status_code == 422


async def test_assigning_to_a_customer_who_does_not_exist_is_a_404(client: AsyncClient, admin_user: User):
    resp = await client.put(
        f"/api/v1/admin/customers/{uuid.uuid4()}/segment",
        headers=auth_header(admin_user),
        json={"segment": "DEALER"},
    )
    assert resp.status_code == 404
