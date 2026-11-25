""""Hesap Türü" in the admin panel: Bireysel ↔ Kurumsal with the firm's
invoice details, the price list left as it is."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import CustomerProfile, CustomerSegment, SegmentType, User
from tests.conftest import auth_header

VKN = "1234567890"  # 10 digits: a şirket
TCKN = "10000000146"  # a valid 11-digit TCKN: a şahıs firması


def url(user: User) -> str:
    return f"/api/v1/admin/customers/{user.id}/account-type"


async def test_an_individual_on_kafe_becomes_corporate_and_keeps_kafe(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, segments
):
    h = auth_header(admin_user)
    user_id = customer_user.id
    put_on = await client.put(f"/api/v1/admin/customers/{user_id}/segment", headers=h, json={"segment": "CAFE"})
    assert put_on.status_code == 200, put_on.text

    resp = await client.put(url(customer_user), headers=h, json={
        "kind": "CORPORATE", "business_type": "CAFE", "company_title": "  KARMA İÇECEK SAN.  VE TİC.LTD.ŞTİ ",
        "tax_office": "Kadıköy", "tax_number": f"{VKN[:3]} {VKN[3:]}",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["account_type"] == "CAFE" and body["segment"] == "CAFE"
    assert body["profile"]["billing_entity_type"] == "COMPANY"
    assert body["profile"]["company_title"] == "KARMA İÇECEK SAN. VE TİC.LTD.ŞTİ"
    assert (body["profile"]["tax_office"], body["profile"]["tax_number"]) == ("Kadıköy", VKN)

    # An 11-digit TCKN is a şahıs firması.
    resp = await client.put(url(customer_user), headers=h, json={
        "kind": "CORPORATE", "business_type": "TOBACCO_SHOP", "company_title": "Demir Nargile",
        "tax_office": "Beşiktaş", "tax_number": TCKN,
    })
    assert resp.json()["profile"]["billing_entity_type"] == "SOLE_PROPRIETOR"
    assert resp.json()["account_type"] == "TOBACCO_SHOP"

    # Back to Bireysel: invoiced to the person, the firm's details kept on file, Kafe kept.
    resp = await client.put(url(customer_user), headers=h, json={"kind": "INDIVIDUAL"})
    body = resp.json()
    assert body["account_type"] == "INDIVIDUAL" and body["segment"] == "CAFE"
    assert body["profile"]["billing_entity_type"] is None and body["profile"]["tax_number"] == TCKN


@pytest.mark.parametrize(
    "change",
    [
        {"company_title": None},
        {"tax_office": ""},
        {"tax_number": "123"},
        {"tax_number": "12345678901"},  # 11 digits failing the TCKN checksum
        {"business_type": None},
        {"business_type": "INDIVIDUAL"},
    ],
)
async def test_corporate_needs_the_firms_details(client: AsyncClient, admin_user: User, customer_user: User, change: dict):
    body = {"kind": "CORPORATE", "business_type": "CAFE", "company_title": "Firma", "tax_office": "Kadıköy", "tax_number": VKN}
    resp = await client.put(url(customer_user), headers=auth_header(admin_user), json=body | change)
    assert resp.status_code == 422, resp.text


async def test_only_admins_change_an_account_type(client: AsyncClient, customer_user: User):
    resp = await client.put(url(customer_user), headers=auth_header(customer_user), json={"kind": "INDIVIDUAL"})
    assert resp.status_code == 403
