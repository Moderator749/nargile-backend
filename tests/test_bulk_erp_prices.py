""""Fiyat Giriş Modu": TL ERP prices typed in the products list write the
catalogue's ERP price and, through the product's worksheet rule, its site price.

Covers:
- A worksheet group: ERP + its "uygulanacak oran"; a typed "uygulanacak fiyat"
  gives way to the new ERP price; retail at its ERP price; a group the
  worksheet does not price, at its ERP price
- The worksheet row brought up to date, so applying it again sets the same prices
- The same arithmetic as the worksheet on an after-VAT (INCL) sheet
- No worksheet: the ERP price, at the product's VAT rate
- A cleared cell removes only the ERP price; all or nothing; admins only
- The list's `pricing=true` carries each product's rule and ERP prices
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Product
from app.models.pricing import PricingRecord
from app.models.user import SegmentType, User
from app.schemas.pricing import PricingSheetIn
from app.services.bulk_erp import site_price
from app.services.pricing_sheet import compute_sheet
from tests.conftest import auth_header
from tests.test_pricing_sheet import _prices, _sheet

URL = "/api/v1/admin/products/bulk-erp-prices"
PRICING = "/api/v1/admin/pricing"


def _site(prices, segment):
    rows = prices[segment]
    assert len(rows) == 1
    return rows[0].currency, rows[0].price_excl_tax, rows[0].price_incl_tax


@pytest.fixture
async def worksheet(client: AsyncClient, db_session: AsyncSession, admin_user: User, catalog):
    """The normal product priced by an applied TRY worksheet: +5% uygulanacak
    oran, Bayi and Toptan columns (Toptan with a typed price), no Kafe column."""
    normal_v, _ = catalog
    code = await db_session.scalar(select(Product.base_code).where(Product.id == normal_v.product_id))
    sheet = _sheet(
        product_code=code,
        applied_percent="5",
        columns=[
            {"label": "Bayi Fiyatı", "segment": "DEALER", "percent": "15"},
            {"label": "Toptan Fiyatı", "segment": "WHOLESALE", "percent": "15", "desired": "999"},
        ],
    )
    h = auth_header(admin_user)
    resp = await client.post(f"{PRICING}/apply-to-product", headers=h, json={"file_name": f"Toplu {code}", "sheet": sheet})
    assert resp.status_code == 200, resp.text
    record = resp.json()["record"]
    # Plain values: the tests expire the session's rows to read them afresh.
    yield {
        "variant_id": normal_v.id, "sku": normal_v.sku, "product_id": normal_v.product_id,
        "record_id": record["id"], "headers": h,
    }
    await client.delete(f"{PRICING}/files/{record['file_id']}", headers=h)


async def test_cells_set_the_erp_and_site_prices_through_the_worksheet(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, worksheet
):
    pid = str(worksheet["product_id"])
    h = worksheet["headers"]
    cells = {"DEALER": "100", "WHOLESALE": "200", "CAFE": "300", "RETAIL": "1000"}
    resp = await client.post(URL, headers=h, json={"changes": [{"product_id": pid, "segment": s, "price": p} for s, p in cells.items()]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_segment = {a["segment"]: a for a in body["applied"]}
    # Bayi: 100 + 5% = 105 before VAT (the sheet is EXCL), 126 with 20%.
    assert (by_segment["DEALER"]["site_price_excl"], by_segment["DEALER"]["site_price_incl"]) == ("105.00", "126.00")
    assert by_segment["DEALER"]["from_worksheet"] and by_segment["DEALER"]["applied_percent"] == "5"
    # Toptan: the typed 999 gives way to the new ERP price.
    assert by_segment["WHOLESALE"]["site_price_excl"] == "210.00"
    # Kafe has no column: its ERP price as it is.
    assert (by_segment["CAFE"]["site_price_excl"], by_segment["CAFE"]["from_worksheet"]) == ("300.00", False)
    # Retail never takes the rate.
    assert (by_segment["RETAIL"]["site_price_excl"], by_segment["RETAIL"]["site_price_incl"]) == ("1000.00", "1200.00")

    db_session.expire_all()
    prices = await _prices(db_session, worksheet["variant_id"])
    assert _site(prices, SegmentType.DEALER) == ("TRY", Decimal("105.00"), Decimal("126.00"))
    assert _site(prices, SegmentType.WHOLESALE) == ("TRY", Decimal("210.00"), Decimal("252.00"))
    assert _site(prices, SegmentType.CAFE) == ("TRY", Decimal("300.00"), Decimal("360.00"))
    assert _site(prices, SegmentType.RETAIL) == ("TRY", Decimal("1000.00"), Decimal("1200.00"))
    product = await db_session.get(Product, worksheet["product_id"])
    assert product.erp_prices["TRY"] == {"DEALER": "100.00", "WHOLESALE": "200.00", "CAFE": "300.00", "RETAIL": "1000.00"}

    # The shopper's (retail) price on the storefront is the new one.
    shop = (await client.get("/api/v1/products/", params={"q": worksheet["sku"]})).json()[0]
    assert Decimal(str(shop["variants"][0]["price"]["price_incl_tax"])) == Decimal("1200.00")

    # The worksheet row now holds the ERP prices, and applying it again changes nothing.
    record = await db_session.get(PricingRecord, worksheet["record_id"])
    columns = {c["segment"]: c for c in record.sheet["columns"]}
    assert (columns["DEALER"]["erp_price"], columns["DEALER"]["price"]) == ("100.00", "105.00")
    assert (columns["WHOLESALE"]["desired"], columns["WHOLESALE"]["price"]) == (None, "210.00")
    assert record.sheet["retail"]["erp_price"] == "1200.00"
    assert record.applied_at >= record.updated_at
    summary = {p["segment"]: p for p in record.applied_summary["prices"]}
    assert summary["DEALER"]["price_excl_tax"] == "105.00" and "CAFE" not in summary

    rows = (await client.get("/api/v1/admin/products", headers=h, params={"q": worksheet["sku"], "pricing": "true"})).json()
    assert rows[0]["price_rule"]["record_id"] == worksheet["record_id"]
    assert rows[0]["price_rule"]["applied_percent"] == "5"
    assert rows[0]["price_rule"]["segments"] == ["DEALER", "WHOLESALE", "RETAIL"]
    assert rows[0]["erp_prices"]["TRY"]["DEALER"] == "100.00"
    # Without the flag, no rule is looked up.
    plain = (await client.get("/api/v1/admin/products", headers=h, params={"q": worksheet["sku"]})).json()
    assert plain[0]["price_rule"] is None


def test_an_after_vat_sheet_prices_as_the_worksheet_does():
    sheet = PricingSheetIn.model_validate(_sheet(
        vat_mode="INCL", applied_percent="7.5",
        columns=[{"label": "Bayi", "segment": "DEALER", "percent": "15", "erp_price": "120.00"}],
    ))
    worksheet_price = Decimal(compute_sheet(sheet)["columns"][0]["price"])
    # ERP 100 before VAT is 120 after: the column's ERP price on an INCL sheet.
    ours = site_price(Decimal("100"), SegmentType.DEALER, compute_sheet(sheet), Decimal("20"))
    assert ours.incl == worksheet_price == Decimal("129.00")
    assert ours.sheet_erp == Decimal("120.00")


async def test_without_a_worksheet_the_site_price_is_the_erp_price(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, catalog
):
    normal_v, _ = catalog
    vid = normal_v.id
    resp = await client.post(
        URL, headers=auth_header(admin_user),
        json={"changes": [{"product_id": str(normal_v.product_id), "segment": "DEALER", "price": "50"}]},
    )
    assert resp.status_code == 200, resp.text
    applied = resp.json()["applied"][0]
    assert applied["from_worksheet"] is False and applied["applied_percent"] is None
    db_session.expire_all()
    # At the product's own VAT rate (18% on its retail row).
    assert _site(await _prices(db_session, vid), SegmentType.DEALER) == ("TRY", Decimal("50.00"), Decimal("59.00"))


async def test_a_cleared_cell_removes_only_the_erp_price(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, catalog
):
    normal_v, _ = catalog
    h = auth_header(admin_user)
    vid, product_id = normal_v.id, normal_v.product_id
    pid = str(product_id)
    await client.post(URL, headers=h, json={"changes": [{"product_id": pid, "segment": "RETAIL", "price": "4000"}]})
    resp = await client.post(URL, headers=h, json={"changes": [{"product_id": pid, "segment": "RETAIL", "price": None}]})
    assert resp.status_code == 200 and resp.json()["applied"][0]["site_price_incl"] is None
    db_session.expire_all()
    assert (await db_session.get(Product, product_id)).erp_prices is None
    # The site price stays the one the earlier cell set.
    assert _site(await _prices(db_session, vid), SegmentType.RETAIL)[1] == Decimal("4000.00")


async def test_all_or_nothing_and_admins_only(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, customer_user: User, catalog
):
    normal_v, _ = catalog
    vid, product_id = normal_v.id, normal_v.product_id
    pid = str(product_id)
    h, customer = auth_header(admin_user), auth_header(customer_user)
    stranger = "00000000-0000-0000-0000-000000000001"
    resp = await client.post(URL, headers=h, json={"changes": [
        {"product_id": pid, "segment": "DEALER", "price": "77"},
        {"product_id": stranger, "segment": "DEALER", "price": "77"},
    ]})
    assert resp.status_code == 422 and "bulunamadı" in resp.text
    db_session.expire_all()
    assert (await db_session.get(Product, product_id)).erp_prices is None
    assert SegmentType.DEALER not in await _prices(db_session, vid)

    twice = [{"product_id": pid, "segment": "CAFE", "price": "1"}] * 2
    assert (await client.post(URL, headers=h, json={"changes": twice})).status_code == 422
    for bad in ("0", "-5", "1.234", "abc"):
        cell = {"product_id": pid, "segment": "CAFE", "price": bad}
        assert (await client.post(URL, headers=h, json={"changes": [cell]})).status_code == 422, bad
    cell = {"product_id": pid, "segment": "CAFE", "price": "10"}
    assert (await client.post(URL, headers=customer, json={"changes": [cell]})).status_code == 403
