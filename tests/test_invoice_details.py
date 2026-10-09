"""A retail customer's invoice details: kept on file, and used on an order.

A bireysel buyer can have the invoice made out to a firm — their own company,
their employer, the firm they are buying for — while the goods go wherever they
like. Two things follow, and these tests walk both:

- the account remembers the firm ("Düzenle" at checkout saves it), so the next
  cart starts filled in; placing an order never writes back by itself, since one
  order's invoice is not a decision about the account;
- the order freezes its own copy of the invoice *and* of the delivery address,
  so corporate invoicing works with delivery to the firm, to home, to a Yurtiçi
  branch or to an address typed once.
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice, WarehouseType
from app.models.user import AddressKind, BillingEntity, CustomerProfile, CustomerSegment, SegmentType, User
from tests.conftest import CONSENTS, auth_header

pytestmark = pytest.mark.asyncio

PRICE = Decimal("5000.00")  # over the free-shipping threshold: no consent in the way

HOME = {
    "street": "Moda Cad.",
    "building_no": "5",
    "district": "Kadıköy",
    "city": "İstanbul",
}
# What the cart's "Faturayı firmaya/şirkete istiyorum" section collects.
FIRM = {
    "company_title": "  Köşe  Kafe Gıda San. ve Tic. Ltd. Şti. ",
    "trade_name": "  Köşe Kafe ",
    "address": {
        "city": "istanbul",
        "district": " Şişli ",
        "line": " Levent Mah., Büyükdere  Caddesi No: 120, Kat 4 ",
    },
    "tax_office": " Beşiktaş ",
    "tax_number": "123 456 7890",
}
SAVED_FIRM = {
    "company_title": "Köşe Kafe Gıda San. ve Tic. Ltd. Şti.",
    "trade_name": "Köşe Kafe",
    "address": {
        "city": "İstanbul",
        "district": "Şişli",
        "line": "Levent Mah., Büyükdere Caddesi No: 120, Kat 4",
    },
    "tax_office": "Beşiktaş",
    "tax_number": "1234567890",
}
HOME_DELIVERY = {
    "full_name": "Live Bireysel",
    "phone": "0532 111 22 33",
    "country": "TR",
    "city": "İstanbul",
    "district": "Kadıköy",
    "line": "Moda Cad. No: 5",
}


async def _variant(db: AsyncSession, segments: dict[SegmentType, CustomerSegment]) -> ProductVariant:
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"INV-{suffix}", name=f"Fatura Testi {suffix}", tags=[])
    db.add(product)
    await db.flush()
    variant = ProductVariant(product_id=product.id, sku=f"INV-{suffix}")
    db.add(variant)
    await db.flush()
    for code in SegmentType:
        db.add(VariantPrice(
            variant_id=variant.id, segment_id=segments[code].id, currency="TRY",
            price_excl_tax=(PRICE / Decimal("1.20")).quantize(Decimal("0.01")),
            price_incl_tax=PRICE, vat_rate=Decimal("20.00"),
        ))
    for warehouse in WarehouseType:
        db.add(Inventory(variant_id=variant.id, warehouse_type=warehouse, quantity=20))
    await db.commit()
    return variant


async def _signed_up(client: AsyncClient, account_type: str = "INDIVIDUAL") -> dict[str, str]:
    """An account that came through the sign-up form, so it has a profile."""
    email = f"inv-{uuid.uuid4().hex[:8]}@test.com"
    body: dict = {
        "account_type": account_type,
        "email": email,
        "password": "Gizli-Parola-123",
        "billing_address": HOME,
    }
    if account_type == "INDIVIDUAL":
        body |= {"full_name": "Live Bireysel", "phone": "0532 111 22 33"}
    else:
        body |= {
            "contact_name": "Mehmet Demir",
            "phone": "0532 444 55 66",
            "company": {
                "entity_type": "COMPANY", "title": "Başka Kafe Ltd.",
                "tax_plate_name": "Başka Kafe Ltd.", "tax_office": "Kadıköy",
                "tax_number": "1234567890",
            },
        }
    resp = await client.post("/api/v1/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "Gizli-Parola-123"}
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _order(variant: ProductVariant, **extra) -> dict:
    return {
        **CONSENTS,
        "payment_method": "CREDIT_CARD",
        "items": [{"variant_id": str(variant.id), "quantity": 1}],
        "shipping_method": "YURTICI_DOOR",
        "shipping_consent": True,
        "shipping_address": HOME_DELIVERY,
        **extra,
    }


CORPORATE_INVOICE = {
    "invoice_to_company": True,
    "invoice_company_title": SAVED_FIRM["company_title"],
    "invoice_trade_name": "  Köşe  Kafe ",
    "invoice_tax_office": "Beşiktaş",
    "invoice_tax_number": "1234567890",
    "invoice_address": SAVED_FIRM["address"],
}


# ── Kept on file ────────────────────────────────────────────────────────────


async def test_nothing_is_on_file_until_the_customer_says_so(client: AsyncClient):
    headers = await _signed_up(client)
    resp = await client.get("/api/v1/me/corporate-invoice", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() is None


async def test_the_firm_is_saved_and_comes_back_for_the_next_order(
    client: AsyncClient, db_session: AsyncSession
):
    headers = await _signed_up(client)
    saved = await client.put("/api/v1/me/corporate-invoice", headers=headers, json=FIRM)
    assert saved.status_code == 200, saved.text
    assert saved.json() == SAVED_FIRM

    # The next cart reads it back, tidied, without asking again.
    assert (await client.get("/api/v1/me/corporate-invoice", headers=headers)).json() == SAVED_FIRM

    # It is a real address row, so the delivery step can offer it too.
    rows = (await client.get("/api/v1/me/addresses", headers=headers)).json()
    assert [row["kind"] for row in rows] == ["BILLING", "CORPORATE_BILLING"]
    corporate = rows[1]
    assert (corporate["city"], corporate["district"]) == ("İstanbul", "Şişli")
    assert corporate["line"] == SAVED_FIRM["address"]["line"]
    assert corporate["label"] == "Kurumsal Fatura Adresi"
    # And the invoice identity sits on the profile, where invoices are made from.
    profile = await db_session.scalar(
        select(CustomerProfile).where(CustomerProfile.company_title == SAVED_FIRM["company_title"])
    )
    await db_session.refresh(profile)
    assert (profile.trade_name, profile.tax_office) == ("Köşe Kafe", "Beşiktaş")
    assert profile.billing_entity_type == BillingEntity.COMPANY


async def test_saving_it_again_replaces_it(client: AsyncClient):
    headers = await _signed_up(client)
    await client.put("/api/v1/me/corporate-invoice", headers=headers, json=FIRM)
    again = await client.put("/api/v1/me/corporate-invoice", headers=headers, json={
        **FIRM,
        "company_title": "Yeni Ünvan A.Ş.",
        "address": {"city": "Ankara", "district": "Çankaya", "line": "Kızılay Mah., No: 9"},
    })
    assert again.status_code == 200, again.text
    assert again.json()["company_title"] == "Yeni Ünvan A.Ş."
    rows = (await client.get("/api/v1/me/addresses", headers=headers)).json()
    # One row per kind, corrected rather than duplicated.
    assert [row["kind"] for row in rows].count("CORPORATE_BILLING") == 1
    assert rows[1]["city"] == "Ankara"


async def test_a_tckn_makes_it_a_sole_proprietorship(
    client: AsyncClient, db_session: AsyncSession
):
    """A person's number means a şahıs firması, which is not something the
    customer should have to answer twice."""
    headers = await _signed_up(client)
    saved = await client.put(
        "/api/v1/me/corporate-invoice", headers=headers,
        json={**FIRM, "company_title": "Ayşe Yıldız", "tax_number": "10000000146"},
    )
    assert saved.status_code == 200, saved.text
    profile = await db_session.scalar(
        select(CustomerProfile).where(CustomerProfile.company_title == "Ayşe Yıldız")
    )
    await db_session.refresh(profile)
    assert profile.billing_entity_type == BillingEntity.SOLE_PROPRIETOR
    assert profile.tax_number == "10000000146"


async def test_the_personal_invoice_address_can_be_corrected(client: AsyncClient):
    headers = await _signed_up(client)
    saved = await client.put("/api/v1/me/billing-address", headers=headers, json={
        "city": "izmir", "district": " Karşıyaka ", "line": " Bostanlı Mah., Cemal Gürsel Cad. No: 7 ",
    })
    assert saved.status_code == 200, saved.text
    assert saved.json() == {
        "city": "İzmir", "district": "Karşıyaka", "line": "Bostanlı Mah., Cemal Gürsel Cad. No: 7",
    }
    rows = (await client.get("/api/v1/me/addresses", headers=headers)).json()
    billing = next(row for row in rows if row["kind"] == "BILLING")
    assert (billing["city"], billing["district"]) == ("İzmir", "Karşıyaka")
    assert billing["line"] == "Bostanlı Mah., Cemal Gürsel Cad. No: 7"


@pytest.mark.parametrize(
    "patch",
    [
        pytest.param({"company_title": "A"}, id="a title too short to be one"),
        pytest.param({"tax_number": "123"}, id="a mistyped tax number"),
        pytest.param({"tax_office": " "}, id="no tax office"),
        pytest.param({"address": {**FIRM["address"], "city": "Münih"}}, id="not a province"),
        pytest.param({"address": {**FIRM["address"], "line": "A"}}, id="no address"),
    ],
)
async def test_half_a_firm_is_not_saved(client: AsyncClient, patch: dict):
    headers = await _signed_up(client)
    resp = await client.put("/api/v1/me/corporate-invoice", headers=headers, json={**FIRM, **patch})
    assert resp.status_code == 422, resp.text
    assert (await client.get("/api/v1/me/corporate-invoice", headers=headers)).json() is None


async def test_a_firms_own_details_are_not_rewritten_from_the_cart(client: AsyncClient):
    """A business account is invoiced to the firm it registered; that is sign-up's
    answer, not checkout's."""
    headers = await _signed_up(client, account_type="CAFE")
    assert (await client.put("/api/v1/me/corporate-invoice", headers=headers, json=FIRM)).status_code == 403
    billing = await client.put(
        "/api/v1/me/billing-address", headers=headers,
        json={"city": "Ankara", "district": "Çankaya", "line": "Kızılay Mah., No: 9"},
    )
    assert billing.status_code == 403, billing.text


async def test_invoice_details_need_a_login(client: AsyncClient):
    assert (await client.get("/api/v1/me/corporate-invoice")).status_code == 401
    assert (await client.put("/api/v1/me/corporate-invoice", json=FIRM)).status_code == 401


# ── On the order ────────────────────────────────────────────────────────────


async def test_a_corporate_invoice_with_delivery_to_the_customers_home(
    client: AsyncClient, db_session: AsyncSession, segments, admin_user: User
):
    headers = await _signed_up(client)
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout", headers=headers, json=_order(variant, **CORPORATE_INVOICE)
    )
    assert placed.status_code == 201, placed.text
    order = placed.json()
    assert order["invoice_trade_name"] == "Köşe Kafe"
    # The invoice is the firm's, in Şişli; the parcel is the buyer's, in Kadıköy.
    assert order["invoice_address"]["district"] == "Şişli"
    assert order["shipping_address"]["district"] == "Kadıköy"
    assert order["shipping_address"]["full_name"] == "Live Bireysel"

    rows = await client.get("/api/v1/admin/orders", headers=auth_header(admin_user))
    row = next(o for o in rows.json() if o["id"] == order["id"])
    assert row["invoice_trade_name"] == "Köşe Kafe"
    assert row["billing_address"]["city"] == "İstanbul"
    assert row["shipping_address"]["district"] == "Kadıköy"


async def test_a_corporate_invoice_with_delivery_to_the_firm(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """"Kurumsal fatura adresime istiyorum": the same place, still two snapshots —
    the parcel is addressed to the firm and carries a phone, the invoice does
    not."""
    headers = await _signed_up(client)
    variant = await _variant(db_session, segments)
    firm_address = SAVED_FIRM["address"]
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=headers,
        json=_order(
            variant,
            **CORPORATE_INVOICE,
            shipping_address={
                "full_name": SAVED_FIRM["company_title"],
                "phone": "0532 111 22 33",
                "country": "TR",
                **firm_address,
            },
        ),
    )
    assert placed.status_code == 201, placed.text
    order = placed.json()
    assert order["invoice_address"] == firm_address
    assert (order["shipping_address"]["city"], order["shipping_address"]["district"]) == (
        firm_address["city"], firm_address["district"],
    )
    assert order["shipping_address"]["full_name"] == SAVED_FIRM["company_title"]
    # The invoice snapshot has no name or number of its own: it is a place.
    assert set(order["invoice_address"]) == {"city", "district", "line"}


async def test_a_branch_parcel_can_still_be_invoiced_to_the_firm(
    client: AsyncClient, db_session: AsyncSession, segments
):
    headers = await _signed_up(client)
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=headers,
        json=_order(
            variant, **CORPORATE_INVOICE,
            shipping_method="YURTICI_BRANCH", shipping_contract_no="YK-4477",
        ),
    )
    assert placed.status_code == 201, placed.text
    order = placed.json()
    assert (order["shipping_method"], order["shipping_contract_no"]) == ("YURTICI_BRANCH", "YK-4477")
    assert order["invoice_company_title"] == SAVED_FIRM["company_title"]


async def test_ordering_does_not_rewrite_what_is_on_file(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """One order's invoice is not a decision about the account: the cart's
    "Düzenle" is what saves, and nothing else."""
    headers = await _signed_up(client)
    variant = await _variant(db_session, segments)
    placed = await client.post(
        "/api/v1/orders/checkout", headers=headers, json=_order(variant, **CORPORATE_INVOICE)
    )
    assert placed.status_code == 201, placed.text
    assert (await client.get("/api/v1/me/corporate-invoice", headers=headers)).json() is None
    rows = (await client.get("/api/v1/me/addresses", headers=headers)).json()
    assert [row["kind"] for row in rows] == ["BILLING"]


async def test_a_saved_firm_is_what_the_next_order_sends(
    client: AsyncClient, db_session: AsyncSession, segments
):
    """The round trip the customer actually sees: save it once, and the details
    the next order carries are the saved ones."""
    headers = await _signed_up(client)
    variant = await _variant(db_session, segments)
    await client.put("/api/v1/me/corporate-invoice", headers=headers, json=FIRM)
    on_file = (await client.get("/api/v1/me/corporate-invoice", headers=headers)).json()

    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=headers,
        json=_order(
            variant,
            invoice_to_company=True,
            invoice_company_title=on_file["company_title"],
            invoice_trade_name=on_file["trade_name"],
            invoice_tax_office=on_file["tax_office"],
            invoice_tax_number=on_file["tax_number"],
            invoice_address=on_file["address"],
        ),
    )
    assert placed.status_code == 201, placed.text
    order = placed.json()
    assert order["invoice_company_title"] == on_file["company_title"]
    assert order["invoice_address"] == on_file["address"]
