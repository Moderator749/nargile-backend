"""MLZ163 (2026-11-24): a deleted product leaves nothing that still takes it
for a live one, and a USD worksheet leaves no stale TL ERP price behind.

Covers:
- ERP prices kept before VAT so VAT put back gives the typed price again:
  0.45 USD at 20% is 0.375 (not 0.38, which shows 0.46); 115 is still 95.83
- A USD worksheet applied over old TL ERP prices: the groups it prices lose
  their TL figures; the product sheet takes and gives back 0.375
- The storefront catalogue's TL line from a USD-only ERP price: converted at
  the day's rate, after VAT, in whole lira
- "Sil": the applied worksheet row is detached (no longer "Sitede", no
  product id), its import aliases go, it is unpinned from cross-sells, its
  flash sale is switched off; the worksheet's photo/ERP lookups and the
  admin category counts ignore it
- The same code, the same explicit address and a copy's "-KOPYA" code are
  free again, and the new product starts with no ERP prices or applied row
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import products as products_api
from app.models.catalog import Category, Product, ProductCrossSell, ProductVariant
from app.models.pricing import PriceImportAlias, PricingRecord
from app.models.promotion import FlashSale
from app.models.user import SegmentType, User
from tests.conftest import auth_header
from tests.test_price_list import _rates

PRICING = "/api/v1/admin/pricing"
SHEET = "/api/v1/admin/products/sheet"


def test_an_erp_price_typed_after_vat_comes_back_exactly():
    from app.services.pricing_engine import erp_before_vat, erp_text

    # 0.45 / 1.20 = 0.375: to the kuruş 0.38, and 0.38 × 1.20 = 0.456 → 0.46.
    assert erp_before_vat(Decimal("0.45"), Decimal("20")) == Decimal("0.3750")
    assert erp_text(erp_before_vat(Decimal("0.45"), Decimal("20"))) == "0.375"
    # Where the kuruş already gives it back, the kuruş it stays.
    assert erp_before_vat(Decimal("115"), Decimal("20")) == Decimal("95.83")
    assert erp_text(Decimal("95.83")) == "95.83" and erp_text(Decimal("105")) == "105.00"
    assert erp_before_vat(Decimal("12.34"), Decimal("0")) == Decimal("12.34")


def _code() -> str:
    return f"MLZ{uuid.uuid4().hex[:6].upper()}"


def _product_sheet(code: str, **overrides) -> dict:
    return {
        "base_code": code,
        "name": f"Ürün {code}",
        "stock": {"quantity": 5},
        "prices": [{
            "currency": "TRY",
            "cells": {
                "DEALER": {"price_excl_tax": "12.50", "price_incl_tax": "15.00"},
                "WHOLESALE": {"price_excl_tax": "12.50", "price_incl_tax": "15.00"},
                "CAFE": {"price_excl_tax": "16.67", "price_incl_tax": "20.00"},
                "RETAIL": {"price_excl_tax": "37.50", "price_incl_tax": "45.00"},
            },
        }],
        "is_published": True,
    } | overrides


def _usd_sheet(code: str) -> dict:
    """Cetvel 1 in USD, as on MLZ163: ERP 0.15 / 0.15 / 0.20, retail 0.45 on the shelf."""
    return {
        "product_code": code, "product_name": "MLZ163", "vat_mode": "EXCL", "currency": "USD",
        "cost": "0.10", "usd_rate": "50.6026",
        "columns": [
            {"label": "Bayi Fiyatı", "segment": "DEALER", "percent": "50", "erp_price": "0.15"},
            {"label": "Toptan Fiyatı", "segment": "WHOLESALE", "percent": "0", "erp_price": "0.15"},
            {"label": "Kafe Fiyatı", "segment": "CAFE", "percent": "30", "erp_price": "0.20"},
        ],
        "retail": {"percent": "50", "vat_rate": "20", "erp_price": "0.45"},
    }


async def _create(client: AsyncClient, admin: User, body: dict) -> dict:
    resp = await client.post(SHEET, headers=auth_header(admin), json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _apply(client: AsyncClient, admin: User, code: str) -> dict:
    resp = await client.post(
        f"{PRICING}/save", headers=auth_header(admin), json={"sheet": _usd_sheet(code), "apply": True, "mode": "new"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_a_usd_sheet_clears_stale_tl_erp_prices_and_keeps_045(
    client: AsyncClient, db_session: AsyncSession, admin_user: User
):
    code = _code()
    erp_tl = {"TRY": {"DEALER": "15.00", "WHOLESALE": "15.00", "CAFE": "20.00", "RETAIL": "45.00"}}
    created = await _create(
        client, admin_user, _product_sheet(code, erp={"erp_prices": erp_tl, "erp_display_perakende": True})
    )
    await _apply(client, admin_user, code)

    product = await db_session.get(Product, uuid.UUID(created["id"]))
    await db_session.refresh(product)
    # Only USD is left; retail is 0.375, which the catalogue shows as 0.45 after VAT.
    assert product.erp_prices == {"USD": {"DEALER": "0.15", "WHOLESALE": "0.15", "CAFE": "0.20", "RETAIL": "0.375"}}

    # The product sheet reads it back, and saving the sheet as it is keeps it.
    h = auth_header(admin_user)
    sheet = (await client.get(f"/api/v1/admin/products/{product.id}/sheet", headers=h)).json()
    assert sheet["erp"]["erp_prices"] == {"USD": {"DEALER": "0.15", "WHOLESALE": "0.15", "CAFE": "0.20", "RETAIL": "0.375"}}
    saved = await client.put(f"/api/v1/admin/products/{product.id}/sheet", headers=h, json=sheet)
    assert saved.status_code == 200, saved.text
    assert saved.json()["erp"]["erp_prices"]["USD"]["RETAIL"] == "0.375"


async def test_the_tl_catalogue_converts_a_usd_only_erp_price(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, monkeypatch
):
    monkeypatch.setattr(products_api, "get_rates", _rates("50.6026"))
    code = _code()
    created = await _create(client, admin_user, _product_sheet(code))
    await _apply(client, admin_user, code)

    resp = await client.post(
        f"{PRICING}/custom-catalog", headers=auth_header(admin_user),
        json={"product_ids": [created["id"]], "segment": "RETAIL"},
    )
    assert resp.status_code == 200, resp.text
    retail = resp.json()[0]["prices"]["RETAIL"]
    # 0.375 USD + 20% = 0.45 USD × 50.6026 = 22.77 TL → 23 TL, whole lira; before VAT 19.1667.
    assert retail["erp_currency"] == "TRY"
    assert (Decimal(retail["erp_price"]) * Decimal("1.2")).quantize(Decimal("0.01")) == Decimal("23.00")


async def test_deleting_a_product_leaves_no_ghost_behind(
    client: AsyncClient, db_session: AsyncSession, admin_user: User
):
    h = auth_header(admin_user)
    code = _code()
    slug = f"urun-{code.lower()}"
    category = Category(name=f"Kat {code}", slug=f"kat-{code.lower()}")
    db_session.add(category)
    await db_session.commit()
    old = await _create(client, admin_user, _product_sheet(
        code, slug=slug, category_ids=[category.id], image_urls=["https://cdn.example.com/eski.jpg"],
    ))
    old_id = uuid.UUID(old["id"])
    neighbour = await _create(client, admin_user, _product_sheet(_code(), related_product_ids=[old["id"]]))
    applied = await _apply(client, admin_user, code)
    record_id = applied["record"]["id"]
    alias = await client.post(
        f"{PRICING}/import-aliases", headers=h, json={"excel_name": f"{code} ESKI", "product_id": old["id"]}
    )
    assert alias.status_code == 201, alias.text
    variant = await db_session.scalar(select(ProductVariant).where(ProductVariant.product_id == old_id))
    sale = FlashSale(
        variant_id=variant.id, sale_price_incl_tax=Decimal("10"), is_active=True,
        starts_at=datetime.now(timezone.utc), ends_at=datetime.now(timezone.utc) + timedelta(days=2),
    )
    db_session.add(sale)
    await db_session.commit()
    counts = {r["id"]: r["product_count"] for r in (await client.get("/api/v1/admin/categories", headers=h)).json()}
    assert counts[category.id] == 1

    resp = await client.post("/api/v1/admin/products/delete", headers=h, json={"product_ids": [old["id"]]})
    assert resp.status_code == 200 and resp.json()["deleted"] == [old["id"]]

    # The worksheet row stays, as a worksheet, but is applied to nothing now.
    record = await db_session.get(PricingRecord, uuid.UUID(record_id))
    await db_session.refresh(record)
    assert record.applied_at is None
    assert record.applied_summary["product_id"] is None and record.applied_summary["deleted_product_id"] == old["id"]
    # Nothing ties to it: aliases, cross-sells, a running flash sale.
    assert await db_session.scalar(select(PriceImportAlias).where(PriceImportAlias.product_id == old_id)) is None
    assert await db_session.scalar(select(ProductCrossSell).where(ProductCrossSell.related_product_id == old_id)) is None
    await db_session.refresh(sale)
    assert sale.is_active is False
    neighbour_sheet = (await client.get(f"/api/v1/admin/products/{neighbour['id']}/sheet", headers=h)).json()
    assert neighbour_sheet["related_product_ids"] == []
    counts = {r["id"]: r["product_count"] for r in (await client.get("/api/v1/admin/categories", headers=h)).json()}
    assert counts[category.id] == 0
    # The worksheet no longer shows the deleted product's photo or ERP prices for its code.
    rows = {r["id"]: r for r in (await client.get(f"{PRICING}/records", headers=h, params={"all": True})).json()}
    assert rows[record_id]["image_url"] is None and rows[record_id]["product_erp"] is None
    # Pinning or aliasing it again is refused.
    again = await client.post(
        f"{PRICING}/import-aliases", headers=h, json={"excel_name": f"{code} YENI", "product_id": old["id"]}
    )
    assert again.status_code == 404

    # The same code and the same address start a clean product.
    new = await _create(client, admin_user, _product_sheet(code, slug=slug))
    assert new["slug"] == slug and new["erp"]["erp_prices"] == {}
    deleted = await db_session.get(Product, old_id)
    await db_session.refresh(deleted)
    assert deleted.slug.startswith(f"{slug}-silindi-")
    # A worksheet applied now goes to the new product only.
    applied = await _apply(client, admin_user, code)
    assert applied["applied"]["product_id"] == new["id"]
    await db_session.refresh(record)
    assert record.applied_at is None  # the old row stays detached

    # Its sale is on a USD-priced colour now: other tests list every sale without rates.
    await db_session.delete(sale)
    await db_session.commit()


async def test_a_copy_takes_a_deleted_copys_code(client: AsyncClient, admin_user: User):
    h = auth_header(admin_user)
    code = _code()
    source = await _create(client, admin_user, _product_sheet(code))
    first = (await client.post("/api/v1/admin/products/duplicate", headers=h, json={"product_ids": [source["id"]]})).json()
    assert first[0]["base_code"] == f"{code}-KOPYA"
    await client.post("/api/v1/admin/products/delete", headers=h, json={"product_ids": [first[0]["id"]]})
    second = (await client.post("/api/v1/admin/products/duplicate", headers=h, json={"product_ids": [source["id"]]})).json()
    assert second[0]["base_code"] == f"{code}-KOPYA"


def test_the_one_off_fix_repairs_mlz163_but_not_an_admins_later_edit():
    """scripts/fix_erp_and_deleted_leftovers.py, on the figures MLZ163 had."""
    from app.schemas.pricing import PricingSheetIn
    from scripts.fix_erp_and_deleted_leftovers import _fixed_erp

    sheet = PricingSheetIn.model_validate(_usd_sheet("MLZ163"))
    broken = Product(erp_prices={
        "TRY": {"DEALER": "15.00", "WHOLESALE": "15.00", "CAFE": "20.00", "RETAIL": "45.00"},
        "USD": {"DEALER": "0.15", "WHOLESALE": "0.15", "CAFE": "0.20", "RETAIL": "0.38"},
    })
    assert _fixed_erp(broken, sheet) == (
        True, {"USD": {"DEALER": "0.15", "WHOLESALE": "0.15", "CAFE": "0.20", "RETAIL": "0.375"}},
    )
    # Retail typed on the product sheet since (0.50): left as it is, TL retail too.
    edited = Product(erp_prices={"TRY": {"RETAIL": "45.00"}, "USD": {"DEALER": "0.15", "RETAIL": "0.50"}})
    assert _fixed_erp(edited, sheet) == (False, edited.erp_prices)
    # Already right: nothing to do.
    fine = Product(erp_prices={"USD": {"DEALER": "0.15", "WHOLESALE": "0.15", "CAFE": "0.20", "RETAIL": "0.375"}})
    assert _fixed_erp(fine, sheet)[0] is False
