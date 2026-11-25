"""RETAIL TRY prices in whole lira: a converted price, a computed one, and the
stored ones the one-off script rounds (1163.86 → 1164.00, rounded up).

Bayi, Toptan and Kafe keep their kuruş everywhere (2026-11-25): a worksheet's
3.16 before VAT stays 3.16 / 3.79, and a dollar price is converted to the
kuruş, on the storefront and at checkout alike."""

import importlib.util
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1 import products as products_api
from app.models.catalog import Inventory, Product, ProductVariant, VariantPrice, WarehouseType
from app.models.user import CustomerSegment, SegmentType, User
from app.services import checkout_engine
from app.services.exchange_rates import Rates
from app.services.pricing_engine import row_in_try, to_try, try_price, whole_lira
from app.services.pricing_sheet import write_prices
from tests.conftest import ADDRESS, CONSENTS, auth_header
from tests.test_b2b_havale import B2B, _firm, _line, _quote

# $19.40 at 60.0 TRY/USD = 1164.00 exactly; at 59.995 = 1163.903 → 1164.00.
RATES = Rates(bulletin_date=date.today(), try_per_unit={"USD": Decimal("59.995"), "EUR": Decimal("65")})


async def _rates():
    return RATES


@pytest.mark.parametrize(
    ("amount", "whole"),
    [("1163.86", "1164.00"), ("1164.00", "1164.00"), ("0.01", "1.00"), ("999.999", "1000.00"), ("0", "0.00")],
)
def test_whole_lira_rounds_up_and_keeps_whole_amounts(amount: str, whole: str):
    assert whole_lira(Decimal(amount)) == Decimal(whole)


def test_try_price_is_whole_lira_for_retail_only():
    assert try_price(Decimal("3.792"), retail=True) == Decimal("4.00")
    assert try_price(Decimal("3.792"), retail=False) == Decimal("3.79")
    assert try_price(Decimal("3.795"), retail=False) == Decimal("3.80")  # half up, to the kuruş
    assert try_price(Decimal("1164.00"), retail=False) == Decimal("1164.00")


def test_a_converted_retail_price_is_whole_lira_and_lira_stays_as_typed():
    assert to_try(Decimal("19.40"), "USD", RATES, retail=True) == Decimal("1164.00")
    assert to_try(Decimal("1163.86"), "TRY", RATES, retail=True) == Decimal("1163.86")  # TRY is charged as typed
    assert to_try(Decimal("19.40"), "USD", None, retail=True) is None


def test_a_converted_business_price_keeps_its_kurus():
    # 19.40 × 59.995 = 1163.903 → 1163.90, not 1164.00.
    assert to_try(Decimal("19.40"), "USD", RATES, retail=False) == Decimal("1163.90")
    assert to_try(Decimal("3.16"), "TRY", RATES, retail=False) == Decimal("3.16")


def test_the_figure_before_vat_follows_the_whole_price():
    row = VariantPrice(currency="USD", price_excl_tax=Decimal("16.17"), price_incl_tax=Decimal("19.40"), vat_rate=Decimal("20.00"))
    # 1164 after VAT is 970.00 before it — not 16.17 × 59.995 = 970.12 rounded on its own.
    assert row_in_try(row, RATES, retail=True) == (Decimal("970.00"), Decimal("1164.00"))
    # A business group: each figure converted to the kuruş, as entered.
    assert row_in_try(row, RATES, retail=False) == (Decimal("970.12"), Decimal("1163.90"))


@pytest.fixture
async def dollar_product(db_session: AsyncSession, segments: dict[SegmentType, CustomerSegment]):
    """A published product sold at $19.40 incl. 20% VAT to every group, no TRY row."""
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"WL-{suffix}", name=f"Dolar Lüle {suffix}", slug=f"dolar-lule-{suffix}", tags=[], is_published=True)
    db_session.add(product)
    await db_session.flush()
    variant = ProductVariant(product_id=product.id, sku=f"WL-{suffix}")
    db_session.add(variant)
    await db_session.flush()
    for segment in segments.values():
        db_session.add(VariantPrice(
            variant_id=variant.id, segment_id=segment.id, currency="USD",
            price_excl_tax=Decimal("16.17"), price_incl_tax=Decimal("19.40"), vat_rate=Decimal("20.00"),
        ))
    db_session.add(Inventory(variant_id=variant.id, warehouse_type=WarehouseType.RETAIL_STOCK, quantity=20))
    await db_session.commit()
    return product, variant


async def test_the_storefront_shows_a_dollar_price_in_whole_lira(client: AsyncClient, dollar_product, monkeypatch):
    monkeypatch.setattr(products_api, "get_rates", _rates)
    product, _ = dollar_product
    listed = (await client.get("/api/v1/products", params={"ids": str(product.id)})).json()
    price = listed[0]["variants"][0]["price"]
    assert (price["currency"], price["price_incl_tax"], price["price_excl_tax"]) == ("TRY", "1164.00", "970.00")
    # The dollar figure it came from is still shown beside it, as entered.
    assert listed[0]["variants"][0]["origin_price"]["price_incl_tax"] == "19.40"


async def test_checkout_charges_the_whole_lira_price(
    client: AsyncClient, customer_user: User, dollar_product, monkeypatch
):
    monkeypatch.setattr(checkout_engine, "get_rates", _rates)
    _, variant = dollar_product
    placed = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(customer_user),
        json={
            **CONSENTS,
            "payment_method": "BANK_TRANSFER",
            "items": [{"variant_id": str(variant.id), "quantity": 2}],
            "shipping_address": ADDRESS,
            "shipping_consent": True,
        },
    )
    assert placed.status_code == 201, placed.text
    line = placed.json()["items"][0]
    assert Decimal(line["unit_price"]) == Decimal("1164.00")


@pytest.mark.parametrize("segment", B2B, ids=lambda s: s.value)
async def test_a_business_account_sees_and_is_charged_the_kurus_price(
    client: AsyncClient, db_session: AsyncSession, segments, dollar_product, segment, monkeypatch
):
    monkeypatch.setattr(products_api, "get_rates", _rates)
    monkeypatch.setattr(checkout_engine, "get_rates", _rates)
    product, variant = dollar_product
    firm = await _firm(client, db_session, segments, segment)
    listed = (await client.get("/api/v1/products", params={"ids": str(product.id)}, headers=firm)).json()
    shown = listed[0]["variants"][0]
    assert (shown["price"]["price_incl_tax"], shown["price"]["price_excl_tax"]) == ("1163.90", "970.12")
    # The retail list price beside it is still whole lira.
    assert shown["list_price"]["price_incl_tax"] == "1164.00"
    quote = await _quote(client, firm, [(variant, 2)])
    assert Decimal(_line(quote, variant)["unit_price"]) == Decimal("1163.90")


async def test_a_worksheet_keeps_business_kurus_and_rounds_retail(
    db_session: AsyncSession, catalog, segments: dict[SegmentType, CustomerSegment]
):
    normal_v, _ = catalog
    variant_id = normal_v.id
    retail_id = segments[SegmentType.RETAIL].id
    # 3.16 before VAT + 20% = 3.79 for every group, as a worksheet would write it.
    rows = [(seg.id, "TRY", Decimal("3.16"), Decimal("3.79")) for seg in segments.values()]
    await write_prices(db_session, [normal_v], rows, Decimal("20.00"), retail_id=retail_id)
    await db_session.commit()
    stored = {
        r.segment_id: (r.price_excl_tax, r.price_incl_tax)
        for r in (await db_session.scalars(select(VariantPrice).where(VariantPrice.variant_id == variant_id))).all()
    }
    for code in B2B:
        assert stored[segments[code].id] == (Decimal("3.16"), Decimal("3.79")), code
    assert stored[retail_id] == (Decimal("3.33"), Decimal("4.00"))


def _script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "round_try_prices.py"
    spec = importlib.util.spec_from_file_location("round_try_prices", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_script_rounds_stored_retail_prices_once_and_leaves_business_kurus(
    db_session: AsyncSession, catalog, segments: dict[SegmentType, CustomerSegment], tmp_path, monkeypatch
):
    normal_v, _ = catalog
    variant_id = normal_v.id
    dealer, retail_id = segments[SegmentType.DEALER].id, segments[SegmentType.RETAIL].id
    dealer_row = VariantPrice(
        variant_id=variant_id, segment_id=dealer, currency="TRY",
        price_excl_tax=Decimal("969.88"), price_incl_tax=Decimal("1163.86"), vat_rate=Decimal("20.00"),
    )
    db_session.add(dealer_row)
    retail = (await db_session.scalars(
        select(VariantPrice).where(VariantPrice.variant_id == variant_id, VariantPrice.segment_id == retail_id)
    )).one()
    retail.price_excl_tax, retail.price_incl_tax, retail.vat_rate = Decimal("969.88"), Decimal("1163.86"), Decimal("20.00")
    await db_session.commit()
    dealer_id, retail_row_id = dealer_row.id, retail.id

    script = _script()
    monkeypatch.setattr(script, "REPORTS", tmp_path)
    # The test's own connection: the app's engine may hold one from another test's loop.
    monkeypatch.setattr(script, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))

    dry = await script.run(apply=False, refresh=False)
    found = {r["id"] for r in dry["variant_prices"]}
    assert retail_row_id in found and dealer_id not in found
    db_session.expire_all()
    assert (await db_session.get(VariantPrice, retail_row_id)).price_incl_tax == Decimal("1163.86")  # nothing written

    await script.run(apply=True, refresh=False)
    db_session.expire_all()
    stored = await db_session.get(VariantPrice, retail_row_id)
    assert (stored.price_excl_tax, stored.price_incl_tax) == (Decimal("970.00"), Decimal("1164.00"))
    # The dealer's kuruş stay.
    kept = await db_session.get(VariantPrice, dealer_id)
    assert (kept.price_excl_tax, kept.price_incl_tax) == (Decimal("969.88"), Decimal("1163.86"))
    saved = list(tmp_path.glob("round-try-prices-*.json"))
    assert len(saved) == 1 and '"old_incl": "1163.86"' in saved[0].read_text()

    again = await script.run(apply=True, refresh=False)
    assert not any(r["id"] == retail_row_id for r in again["variant_prices"])


def _restore_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "restore_b2b_kurus.py"
    spec = importlib.util.spec_from_file_location("restore_b2b_kurus", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_restore_gives_business_rows_their_kurus_back(
    db_session: AsyncSession, catalog, segments: dict[SegmentType, CustomerSegment], tmp_path, monkeypatch
):
    import json
    from datetime import datetime, timezone

    from app.models.pricing import PricingRecord

    normal_v, heavy_v = catalog
    normal_id, heavy_id, product_id = normal_v.id, heavy_v.id, normal_v.product_id
    seg = {code: s.id for code, s in segments.items()}

    def row(variant_id, code, excl, incl):
        r = VariantPrice(variant_id=variant_id, segment_id=seg[code], currency="TRY",
                         price_excl_tax=Decimal(excl), price_incl_tax=Decimal(incl), vat_rate=Decimal("20.00"))
        db_session.add(r)
        return r

    # A sheet applied after the rounding: Bayi 3.16 before VAT written as 3.33 / 4.00.
    sheet_dealer = row(normal_id, SegmentType.DEALER, "3.33", "4.00")
    # Kafe changed by hand since: left alone.
    sheet_cafe = row(normal_id, SegmentType.CAFE, "9.17", "11.00")
    # Rounded by the one-off script and untouched since; and one changed since.
    report_row = row(heavy_id, SegmentType.WHOLESALE, "34.17", "41.00")
    report_changed = row(heavy_id, SegmentType.CAFE, "50.00", "60.00")
    await db_session.flush()
    ids = {sheet_dealer.id, sheet_cafe.id, report_row.id, report_changed.id}
    dealer_id, cafe_id, report_id, changed_id = sheet_dealer.id, sheet_cafe.id, report_row.id, report_changed.id
    db_session.add(PricingRecord(
        product_code="NRM", product_name="Normal", applied_at=datetime.now(timezone.utc),
        sheet={"cost": "1.5", "currency": "TRY", "vat_mode": "EXCL", "retail": {"vat_rate": "20"}, "columns": [
            {"segment": "DEALER", "price": "3.16"}, {"segment": "CAFE", "price": "5.26"},
        ]},
        applied_summary={"product_id": str(product_id), "prices": [
            {"segment": "DEALER", "currency": "TRY", "price_excl_tax": "3.33", "price_incl_tax": "4.00"},
            {"segment": "CAFE", "currency": "TRY", "price_excl_tax": "5.83", "price_incl_tax": "7.00"},
            {"segment": "RETAIL", "currency": "TRY", "price_excl_tax": "6.67", "price_incl_tax": "8.00"},
        ]},
    ))
    await db_session.commit()
    report = tmp_path / "round-try-prices-x.json"
    report.write_text(json.dumps({"variant_prices": [
        {"id": report_id, "segment_id": seg[SegmentType.WHOLESALE], "old_excl": "33.68", "old_incl": "40.42",
         "new_excl": "34.17", "new_incl": "41.00"},
        {"id": changed_id, "segment_id": seg[SegmentType.CAFE], "old_excl": "44.21", "old_incl": "53.05",
         "new_excl": "45.00", "new_incl": "54.00"},
    ]}))

    script = _restore_script()
    monkeypatch.setattr(script, "REPORTS", tmp_path)
    monkeypatch.setattr(script, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))

    dry = await script.run(apply=False, report=report)
    assert {c["id"] for c in dry["changes"]} & ids == {dealer_id, report_id}
    await script.run(apply=True, report=report)
    db_session.expire_all()

    async def got(row_id):
        stored = await db_session.get(VariantPrice, row_id)
        return stored.price_excl_tax, stored.price_incl_tax

    assert await got(dealer_id) == (Decimal("3.16"), Decimal("3.79"))
    assert await got(report_id) == (Decimal("33.68"), Decimal("40.42"))
    assert await got(cafe_id) == (Decimal("9.17"), Decimal("11.00"))
    assert await got(changed_id) == (Decimal("50.00"), Decimal("60.00"))
    assert list(tmp_path.glob("restore-b2b-kurus-*.json"))
    again = await script.run(apply=True, report=report)
    assert not {c["id"] for c in again["changes"]} & ids
