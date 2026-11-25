"""The worksheet audit's fixes (2026-11-25): rounding half up as the panel
does, ERP prices to four places, the shop's VAT rates only, retail TL in whole
lira from the product form too, one "Sitede" record per product, drift from
the live price flagged, ambiguous codes refused, the old save endpoints gone."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Product, ProductVariant, VariantPrice
from app.models.user import CustomerSegment, SegmentType, User
from app.schemas.pricing import PricingSheetIn
from app.services.pricing_sheet import compute_sheet, erp_by_group
from tests.conftest import auth_header
from tests.pricing_helpers import legacy_save
from tests.test_pricing_sheet import _sheet
from tests.test_product_sheet import _post, _sheet as _product_sheet

BASE = "/api/v1/admin/pricing"


def test_a_half_kurus_rounds_up_as_the_panel_shows_it():
    # 10.10 + 5% = 10.605 exactly: 10.61 on screen, and now saved so too (was 10.60).
    out = compute_sheet(PricingSheetIn.model_validate(_sheet(
        cost="10.10", columns=[{"label": "Bayi", "segment": "DEALER", "percent": "5"}],
    )))
    assert out["columns"][0]["computed"] == out["columns"][0]["price"] == "10.61"


def test_an_erp_price_keeps_four_places_before_vat():
    sheet = PricingSheetIn.model_validate(_sheet(
        columns=[{"label": "Bayi", "segment": "DEALER", "percent": "5", "erp_price": "0.375"}],
    ))
    assert erp_by_group(sheet)[SegmentType.DEALER] == Decimal("0.3750")


@pytest.mark.parametrize("rate", ["15", "18", "8"])
def test_only_the_shops_vat_rates_are_taken(rate: str):
    with pytest.raises(ValidationError):
        PricingSheetIn.model_validate(_sheet(retail={"percent": "20", "vat_rate": rate}))


@pytest.mark.parametrize("rate", ["0", "1", "10", "20"])
def test_the_shops_vat_rates_pass(rate: str):
    PricingSheetIn.model_validate(_sheet(retail={"percent": "20", "vat_rate": rate}))


@pytest.mark.parametrize("path", ["draft", "file", "apply-to-product"])
async def test_the_old_save_endpoints_are_gone(client: AsyncClient, admin_user: User, path: str):
    resp = await client.post(f"{BASE}/{path}", headers=auth_header(admin_user), json={"sheet": _sheet()})
    assert resp.status_code == 410 and "/admin/pricing/save" in resp.json()["detail"]


async def _code(db: AsyncSession, variant: ProductVariant) -> str:
    return await db.scalar(select(Product.base_code).where(Product.id == variant.product_id))


async def test_only_the_latest_applied_record_is_on_the_site_and_drift_is_flagged(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, catalog, segments
):
    normal_v, _ = catalog
    variant_id = normal_v.id
    code = await _code(db_session, normal_v)
    retail_id = segments[SegmentType.RETAIL].id
    h = auth_header(admin_user)
    first = await legacy_save(client, "apply", h, {"file_name": f"İlk {code}", "sheet": _sheet(product_code=code)})
    second = await legacy_save(client, "apply", h, {"file_name": f"İkinci {code}", "sheet": _sheet(product_code=code, cost="1100")})
    assert first.status_code == second.status_code == 200, second.text
    first_id, second_id = first.json()["record"]["id"], second.json()["record"]["id"]

    async def listed() -> dict:
        return {r["id"]: r for r in (await client.get(f"{BASE}/records", headers=h, params={"all": "true"})).json()}

    rows = await listed()
    assert rows[first_id]["superseded"] is True and rows[first_id]["applied_summary"]["superseded_by"] == second_id
    assert rows[second_id]["superseded"] is False and rows[second_id]["site_drift"] is None

    # Retail changed on the product afterwards: the record says so.
    await db_session.execute(
        update(VariantPrice)
        .where(VariantPrice.variant_id == variant_id, VariantPrice.segment_id == retail_id)
        .values(price_incl_tax=Decimal("9999.00"), price_excl_tax=Decimal("8332.50"))
    )
    await db_session.commit()
    rows = await listed()
    assert rows[second_id]["site_drift"] == ["Perakende Fiyat"]
    assert rows[first_id]["site_drift"] is None  # superseded: not compared

    # Applying the first again makes it the site's record, and the second superseded.
    again = await legacy_save(client, "apply", h, {"file_name": f"İlk {code}", "sheet": _sheet(product_code=code), "replace_id": first_id})
    assert again.status_code == 200, again.text
    rows = await listed()
    assert rows[first_id]["superseded"] is False and "superseded_at" not in rows[first_id]["applied_summary"]
    assert rows[second_id]["superseded"] is True


async def test_a_code_two_products_answer_to_is_refused(
    client: AsyncClient, db_session: AsyncSession, admin_user: User
):
    code = f"AMB-{uuid.uuid4().hex[:5]}"
    for base in (code, code.lower()):
        product = Product(base_code=base, name=f"Çift {base}", tags=[])
        db_session.add(product)
        await db_session.flush()
        db_session.add(ProductVariant(product_id=product.id, sku=f"{base}-{uuid.uuid4().hex[:4]}"))
    await db_session.commit()
    h = auth_header(admin_user)
    resp = await legacy_save(client, "apply", h, {"file_name": "Çift", "sheet": _sheet(product_code=code)})
    assert resp.status_code == 422 and "birden fazla ürüne" in resp.json()["detail"]


async def test_the_product_form_keeps_retail_tl_in_whole_lira(client: AsyncClient, admin_user: User):
    ends = datetime.now(timezone.utc) + timedelta(days=3)
    body = _product_sheet(
        prices=[{"currency": "TRY", "cells": {
            "DEALER": {"price_excl_tax": "3.16", "price_incl_tax": "3.79"},
            "WHOLESALE": {"price_excl_tax": "3.16"},
            "CAFE": {"price_incl_tax": "6.31"},
            "RETAIL": {"price_incl_tax": "7.50"},
        }}],
        opportunity={
            "is_opportunity": True, "opportunity_price": "6.40", "opportunity_stock": 5,
            "opportunity_end_date": ends.isoformat(),
        },
    )
    created = await _post(client, admin_user, body)
    assert created.status_code == 201, created.text
    sheet = created.json()
    cells = next(r for r in sheet["prices"] if r["currency"] == "TRY")["cells"]
    got = {seg: (Decimal(c["price_excl_tax"]), Decimal(c["price_incl_tax"])) for seg, c in cells.items()}
    assert got["RETAIL"] == (Decimal("6.67"), Decimal("8.00"))  # 7.50 typed → 8.00
    assert got["DEALER"] == (Decimal("3.16"), Decimal("3.79"))
    assert got["WHOLESALE"] == (Decimal("3.16"), Decimal("3.79"))
    assert got["CAFE"] == (Decimal("5.26"), Decimal("6.31"))
    assert Decimal(sheet["opportunity"]["opportunity_price"]) == Decimal("7.00")  # 6.40 → 7.00


async def test_a_flash_sale_price_is_whole_lira(client: AsyncClient, admin_user: User, catalog):
    normal_v, _ = catalog
    sku = normal_v.sku
    resp = await client.post(
        "/api/v1/admin/flash-sales", headers=auth_header(admin_user),
        json={"sku": sku, "sale_price_incl_tax": "4499.50", "starts_at": datetime.now(timezone.utc).isoformat()},
    )
    assert resp.status_code in (200, 201), resp.text
    assert Decimal(resp.json()["sale_price_incl_tax"]) == Decimal("4500.00")


async def test_a_file_named_with_a_turkish_capital_i_is_found_again(client: AsyncClient, admin_user: User):
    # Python lower-cases "İ" differently from Postgres: the second save used to
    # miss the file and crash on its unique name.
    h = auth_header(admin_user)
    name = f"İndirim {uuid.uuid4().hex[:5]}"
    first = await legacy_save(client, "file", h, {"file_name": name, "sheet": _sheet()})
    again = await legacy_save(client, "file", h, {"file_name": name.upper(), "sheet": _sheet()})
    assert first.status_code == again.status_code == 200, again.text
    assert first.json()["file_id"] == again.json()["file_id"]


async def test_the_one_off_marks_older_applied_records_and_resyncs_restored_kurus(
    db_session: AsyncSession, catalog, tmp_path, monkeypatch
):
    import importlib.util
    import json
    from pathlib import Path

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.pricing import PricingRecord

    normal_v, _ = catalog
    pid = str(normal_v.product_id)
    now = datetime.now(timezone.utc)
    prices = [{"segment": "DEALER", "currency": "TRY", "price_excl_tax": "3.33", "price_incl_tax": "4.00"}]
    # A real sheet's shape: later tests read every applied record's cost.
    SHEET = {"cost": "1.5", "vat_mode": "EXCL", "currency": "TRY", "retail": {"vat_rate": "20"}}
    old = PricingRecord(product_code="X", product_name="X", sheet=SHEET, applied_at=now - timedelta(days=2),
                        applied_summary={"product_id": pid, "prices": prices})
    new = PricingRecord(product_code="X", product_name="X", sheet=SHEET, applied_at=now - timedelta(days=1),
                        applied_summary={"product_id": pid, "prices": prices})
    db_session.add_all([old, new])
    await db_session.commit()
    old_id, new_id = old.id, new.id
    report = tmp_path / "restore-b2b-kurus-x.json"
    report.write_text(json.dumps({"changes": [
        {"source": f"sheet {new_id}", "segment": "DEALER", "new_excl": "3.16", "new_incl": "3.79"},
    ]}))

    path = Path(__file__).resolve().parent.parent / "scripts" / "supersede_applied_records.py"
    spec = importlib.util.spec_from_file_location("supersede_applied_records", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.setattr(script, "REPORTS", tmp_path)
    monkeypatch.setattr(script, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))

    async def noop():
        return None

    from types import SimpleNamespace

    monkeypatch.setattr(script, "engine", SimpleNamespace(dispose=noop))

    await script.run(apply=True, report=report)
    db_session.expire_all()
    got_old, got_new = await db_session.get(PricingRecord, old_id), await db_session.get(PricingRecord, new_id)
    assert got_old.applied_summary["superseded_by"] == str(new_id)
    assert "superseded_at" not in got_new.applied_summary
    assert got_new.applied_summary["prices"][0]["price_incl_tax"] == "3.79"
    assert got_old.applied_summary["prices"][0]["price_incl_tax"] == "4.00"  # not the restore's record

    again = await script.run(apply=True, report=report)
    assert not [c for c in again["changes"] if c["id"] in (str(old_id), str(new_id))]


async def test_a_product_that_keeps_retail_kurus_keeps_7_50(
    client: AsyncClient, db_session: AsyncSession, admin_user: User
):
    """"Perakende TL kuruşlu kalsın": the form, and a worksheet applied over
    it, keep the 7.50 typed; without it the same 7.50 becomes 8.00."""
    body = _product_sheet(
        prices=[{"currency": "TRY", "cells": {
            "DEALER": {"price_excl_tax": "3.16", "price_incl_tax": "3.79"},
            "RETAIL": {"price_excl_tax": "6.25", "price_incl_tax": "7.50"},
        }}],
    )
    body["rules"] = {**body["rules"], "allow_retail_kurus": True}
    created = await _post(client, admin_user, body)
    assert created.status_code == 201, created.text
    sheet = created.json()
    assert sheet["rules"]["allow_retail_kurus"] is True
    retail = next(r for r in sheet["prices"] if r["currency"] == "TRY")["cells"]["RETAIL"]
    assert (Decimal(retail["price_excl_tax"]), Decimal(retail["price_incl_tax"])) == (Decimal("6.25"), Decimal("7.50"))

    # A worksheet applied to it writes its 7.50 as it is, too.
    code = body["base_code"]
    worksheet = _sheet(
        product_code=code, cost="1.5",
        columns=[{"label": "Bayi", "segment": "DEALER", "percent": "100"}],
        retail={"percent": "10", "vat_rate": "20", "desired": "7.50"},
    )
    applied = await legacy_save(client, "apply", auth_header(admin_user), {"file_name": f"Kuruş {code}", "sheet": worksheet})
    assert applied.status_code == 200, applied.text
    written = {p["segment"]: p for p in applied.json()["prices"]}
    assert written["RETAIL"]["price_incl_tax"] == "7.50" and written["RETAIL"]["price_excl_tax"] == "6.25"

    # Switched off again: the same form save rounds it up.
    body["rules"]["allow_retail_kurus"] = False
    product_id = sheet["id"]
    again = await client.put(f"/api/v1/admin/products/{product_id}/sheet", headers=auth_header(admin_user), json=body)
    assert again.status_code == 200, again.text
    retail = next(r for r in again.json()["prices"] if r["currency"] == "TRY")["cells"]["RETAIL"]
    assert Decimal(retail["price_incl_tax"]) == Decimal("8.00")
