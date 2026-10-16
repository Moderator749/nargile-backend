"""The legal gate on checkout, and the texts behind it.

An order is a contract: the two agreements — mesafeli satış sözleşmesi and
şartlar ve kurallar — are not optional, and the shop records when they were
accepted. The texts themselves are rows an admin edits, read live by the cart,
so what the next buyer accepts is what the panel last saved.

The invoice is the other half of the blueprint's bireysel flow: a personal order
can be made out to a firm, which then has to say which firm — its tax identity
and the address it is registered at, kept on the order as its own snapshot so
the delivery address is free to be somewhere else entirely.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.legal_texts import DEFAULT_LEGAL
from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice, WarehouseType
from app.models.user import CustomerSegment, SegmentType, User, UserRole
from tests.conftest import ADDRESS, CONSENTS, auth_header

# The firm's registered address, as the invoice has to show it. Nothing to do
# with where the parcel goes.
INVOICE_ADDRESS = {
    "city": "istanbul",
    "district": "  Beşiktaş ",
    "line": "  Levent Mah., Büyükdere  Caddesi No: 120, Kat 4 ",
}
CORPORATE = {
    "invoice_to_company": True,
    "invoice_company_title": "  Köşe  Kafe Ltd. Şti. ",
    "invoice_tax_office": "Beşiktaş",
    "invoice_tax_number": "123 456 7890",
    "invoice_address": INVOICE_ADDRESS,
}

pytestmark = pytest.mark.asyncio

KEYS = ["PRIVACY_NOTICE", "PRIVACY", "DISTANCE_SALES", "TERMS"]
# Over the free-shipping threshold, so no karşı ödemeli consent is in the way of
# what these tests are actually about.
PRICE = Decimal("5000.00")


async def _variant(db: AsyncSession, segments: dict[SegmentType, CustomerSegment]) -> ProductVariant:
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"LGL-{suffix}", name=f"Sözleşme Testi {suffix}", tags=[])
    db.add(product)
    await db.flush()
    variant = ProductVariant(product_id=product.id, sku=f"LGL-{suffix}")
    db.add(variant)
    await db.flush()
    for code in SegmentType:
        db.add(
            VariantPrice(
                variant_id=variant.id,
                segment_id=segments[code].id,
                currency="TRY",
                price_excl_tax=(PRICE / Decimal("1.20")).quantize(Decimal("0.01")),
                price_incl_tax=PRICE,
                vat_rate=Decimal("20.00"),
            )
        )
    for warehouse in WarehouseType:
        db.add(Inventory(variant_id=variant.id, warehouse_type=warehouse, quantity=20))
    await db.commit()
    return variant


async def _buyer(db: AsyncSession, segments: dict[SegmentType, CustomerSegment]) -> User:
    user = User(
        email=f"legal-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        full_name="Test Alıcı",
        phone="0532 111 22 33",
        role=UserRole.CUSTOMER,
        segment_id=segments[SegmentType.RETAIL].id,
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    await db.commit()
    return user


def _body(variant: ProductVariant, **extra) -> dict:
    return {
        "payment_method": "CREDIT_CARD",
        "items": [{"variant_id": str(variant.id), "quantity": 1}],
        "shipping_address": ADDRESS,
        **extra,
    }


# ── The texts ────────────────────────────────────────────────────────────────


async def test_the_storefront_can_read_the_texts_without_a_login(client: AsyncClient):
    """The cart shows them before anyone has signed in, so they are public."""
    resp = await client.get("/api/v1/settings/legal")
    assert resp.status_code == 200, resp.text
    docs = resp.json()
    assert [d["key"] for d in docs] == KEYS
    # The notice beside the payment step is the text the blueprint fixes.
    notice = next(d for d in docs if d["key"] == "PRIVACY_NOTICE")
    assert notice["body"] == DEFAULT_LEGAL["PRIVACY_NOTICE"][1]  # the seeded text, as HTML
    # Untouched texts say so, rather than reporting the setup date as an edit.
    assert notice["edited"] is False
    assert all(d["body"] for d in docs)


async def test_an_admin_edit_is_what_the_next_buyer_reads(client: AsyncClient, admin_user: User):
    resp = await client.put(
        "/api/v1/admin/settings/legal/TERMS",
        headers=auth_header(admin_user),
        json={"title": "  Şartlar ve Kurallar  ", "body": "  Yeni şartlar metni.  "},
    )
    assert resp.status_code == 200, resp.text
    assert (resp.json()["title"], resp.json()["body"]) == ("Şartlar ve Kurallar", "<p>Yeni şartlar metni.</p>")
    assert resp.json()["updated_at"] is not None
    assert resp.json()["edited"] is True

    public = await client.get("/api/v1/settings/legal")
    terms = next(d for d in public.json() if d["key"] == "TERMS")
    assert terms["body"] == "<p>Yeni şartlar metni.</p>"
    # The documents nobody touched still read as seeded.
    privacy = next(d for d in public.json() if d["key"] == "PRIVACY")
    assert privacy["body"] == DEFAULT_LEGAL["PRIVACY"][1]


async def test_a_text_cannot_be_emptied(client: AsyncClient, admin_user: User):
    """An empty body would leave the cart with a blank agreement to accept."""
    for payload in ({"title": "Şartlar", "body": "   "}, {"title": "", "body": "Metin"}):
        resp = await client.put(
            "/api/v1/admin/settings/legal/TERMS", headers=auth_header(admin_user), json=payload
        )
        assert resp.status_code == 422, resp.text


async def test_only_an_admin_writes_them(client: AsyncClient, customer_user: User):
    body = {"title": "Şartlar", "body": "Kendi şartlarım"}
    assert (await client.put("/api/v1/admin/settings/legal/TERMS", json=body)).status_code == 401
    assert (
        await client.put(
            "/api/v1/admin/settings/legal/TERMS", headers=auth_header(customer_user), json=body
        )
    ).status_code == 403
    assert (await client.get("/api/v1/admin/settings/legal")).status_code == 401


async def test_an_unknown_text_is_not_invented(client: AsyncClient, admin_user: User):
    resp = await client.put(
        "/api/v1/admin/settings/legal/KENDI_METNIM",
        headers=auth_header(admin_user),
        json={"title": "Kendi metnim", "body": "Metin"},
    )
    assert resp.status_code == 422, resp.text


# ── The gate ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("missing", "expected"),
    [
        pytest.param({}, "Mesafeli Satış", id="neither accepted"),
        pytest.param({"terms_accepted": True}, "Mesafeli Satış", id="only the terms"),
        pytest.param({"distance_sales_accepted": True}, "Şartlar ve Kurallar", id="only the contract"),
    ],
)
async def test_an_order_without_both_agreements_is_refused(
    client: AsyncClient, db_session: AsyncSession, segments, missing: dict, expected: str
):
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    resp = await client.post(
        "/api/v1/orders/checkout", headers=auth_header(user), json=_body(variant, **missing)
    )
    assert resp.status_code == 422, resp.text
    assert expected in resp.json()["detail"]


async def test_an_accepted_order_records_when(
    client: AsyncClient, db_session: AsyncSession, segments
):
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout", headers=auth_header(user), json=_body(variant, **CONSENTS)
    )
    assert placed.status_code == 201, placed.text
    assert placed.json()["agreements_accepted_at"] is not None


async def test_looking_at_the_price_asks_for_nothing(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """A cart preview is not an order: it quotes without any agreement."""
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    quote = await client.post(
        "/api/v1/orders/calculate-cart", headers=auth_header(user), json=_body(variant)
    )
    assert quote.status_code == 200, quote.text


# ── The invoice ──────────────────────────────────────────────────────────────


async def test_a_personal_order_can_be_invoiced_to_a_firm(
    client: AsyncClient, db_session: AsyncSession, segments, admin_user: User
):
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(user),
        json=_body(variant, **CONSENTS, **CORPORATE, save_card=True),
    )
    assert placed.status_code == 201, placed.text
    order = placed.json()
    assert order["invoice_to_company"] is True
    assert order["invoice_company_title"] == "Köşe Kafe Ltd. Şti."
    assert (order["invoice_tax_office"], order["invoice_tax_number"]) == ("Beşiktaş", "1234567890")
    # The firm's own address, tidied and held to the 81 provinces.
    assert order["invoice_address"] == {
        "city": "İstanbul",
        "district": "Beşiktaş",
        "line": "Levent Mah., Büyükdere Caddesi No: 120, Kat 4",
    }
    # And it did not become the delivery address: the parcel still goes to Ankara.
    assert order["shipping_address"]["city"] == ADDRESS["city"]
    # The buyer asked for their card to be kept; we never hold the card itself.
    assert order["save_card"] is True

    # The shop has to be able to make the invoice out.
    rows = await client.get("/api/v1/admin/orders", headers=auth_header(admin_user))
    row = next(o for o in rows.json() if o["id"] == order["id"])
    assert row["invoice_company_title"] == "Köşe Kafe Ltd. Şti."
    assert row["invoice_tax_number"] == "1234567890"
    # The sheet's "Fatura Adresi" block prints this order's own snapshot, made
    # out to the firm, rather than whatever the account says today.
    assert row["billing_address"]["full_name"] == "Köşe Kafe Ltd. Şti."
    assert (row["billing_address"]["city"], row["billing_address"]["district"]) == (
        "İstanbul", "Beşiktaş",
    )


def _without(*fields: str) -> dict:
    """The corporate invoice, with some of it missing."""
    invoice = {k: v for k, v in CORPORATE.items() if k not in fields}
    invoice["invoice_to_company"] = True
    return invoice


@pytest.mark.parametrize(
    ("invoice", "expected"),
    [
        pytest.param(_without("invoice_company_title"), "ünvan", id="no title"),
        pytest.param(_without("invoice_tax_office"), "vergi dairesi", id="no tax office"),
        pytest.param(_without("invoice_tax_number"), "10 (VKN)", id="no tax number"),
        pytest.param(
            {**CORPORATE, "invoice_tax_number": "123"}, "10 (VKN)", id="a mistyped tax number",
        ),
        pytest.param(_without("invoice_address"), "fatura adresini", id="no address"),
        pytest.param(
            {**CORPORATE, "invoice_address": {**INVOICE_ADDRESS, "city": "Münih"}},
            "81 ilden biri",
            id="somewhere that is not a province",
        ),
        pytest.param(
            {**CORPORATE, "invoice_address": {**INVOICE_ADDRESS, "district": " "}},
            "ilçe girin",
            id="no ilçe",
        ),
        pytest.param(
            {**CORPORATE, "invoice_address": {**INVOICE_ADDRESS, "line": "  "}},
            "açık fatura adresini",
            id="no open address",
        ),
    ],
)
async def test_an_invoice_to_a_firm_has_to_say_which_firm_and_where(
    client: AsyncClient, db_session: AsyncSession, segments, invoice: dict, expected: str
):
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    resp = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(user),
        json=_body(variant, **CONSENTS, **invoice),
    )
    assert resp.status_code == 422, resp.text
    assert expected in resp.text


async def test_a_sole_proprietor_is_invoiced_under_a_tckn(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """A şahıs şirketi has no VKN of its own, so its 11-digit TCKN stands in —
    checksum and all, which is what tells a typo from a number."""
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(user),
        json=_body(variant, **CONSENTS, **{**CORPORATE, "invoice_tax_number": "10000000146"}),
    )
    assert placed.status_code == 201, placed.text
    assert placed.json()["invoice_tax_number"] == "10000000146"

    refused = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(user),
        json=_body(variant, **CONSENTS, **{**CORPORATE, "invoice_tax_number": "10000000147"}),
    )
    assert refused.status_code == 422, refused.text


async def test_the_invoice_address_is_not_the_delivery_address(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """The whole point of the two snapshots: a buyer writes the purchase off to
    their firm in İstanbul and has the parcel sent to a branch in Ankara."""
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(user),
        json=_body(
            variant, **CONSENTS, **CORPORATE,
            shipping_address={**ADDRESS, "city": "Ankara", "district": "Çankaya"},
        ),
    )
    assert placed.status_code == 201, placed.text
    order = placed.json()
    assert (order["invoice_address"]["city"], order["shipping_address"]["city"]) == (
        "İstanbul", "Ankara",
    )


async def test_details_left_from_an_unticked_box_are_forgotten(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """Changing your mind back to a personal invoice is not an error."""
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(user),
        json=_body(
            variant, **CONSENTS, invoice_company_title="Hayalet Ltd.",
            invoice_tax_number="1234567890", invoice_address=INVOICE_ADDRESS,
        ),
    )
    assert placed.status_code == 201, placed.text
    assert placed.json()["invoice_to_company"] is False
    assert placed.json()["invoice_company_title"] is None
    assert placed.json()["invoice_tax_number"] is None
    assert placed.json()["invoice_address"] is None


async def test_a_branch_parcel_can_carry_the_buyers_own_yurtici_agreement(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """"Size ait anlaşma varsa kodunuzu girebilirsiniz" sits under the branch
    option, so the agreement belongs to the carrier and not to one of its two
    ways of delivering."""
    user = await _buyer(db_session, segments)
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(user),
        json=_body(
            variant, **CONSENTS, shipping_method="YURTICI_BRANCH", shipping_contract_no=" YK-5512 "
        ),
    )
    assert placed.status_code == 201, placed.text
    assert placed.json()["shipping_contract_no"] == "YK-5512"
