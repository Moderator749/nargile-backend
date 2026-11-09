"""Fiyat Hesaplama Cetveli.

Covers:
- The sequential arithmetic: 1000 → +15% → 1150 → +15% → 1322.50 → +20% → 1587,
  and retail after VAT on top of it (1587 + %20 KDV = 1904.40)
- A typed "uygulanacak fiyat" is the column's price but never moves the calculated chain
- USD/EUR sheets: the TL figure at the typed rate
- Drafts and files: saving, reusing a file by name, listing, deleting
- Picked rows deleted without their file, moved to another file; files renamed
  (a taken name refused) and duplicated with every row
- Applying to a product: every customer group's live price, both VAT sides,
  other-currency rows removed, "son karar" as the TRY retail price
- A product that cannot be found saves nothing; admins only; bad sheets refused
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Product, ProductVariant, VariantPrice
from app.models.pricing import PricingRecord
from app.models.user import CustomerSegment, SegmentType, User
from app.schemas.pricing import PricingSheetIn
from app.services.pricing_sheet import compute_sheet
from tests.conftest import auth_header

BASE = "/api/v1/admin/pricing"


def _sheet(**overrides) -> dict:
    sheet = {
        "product_code": "",
        "product_name": "Deneme",
        "vat_mode": "EXCL",
        "currency": "TRY",
        "cost": "1000",
        "columns": [
            {"label": "Bayi Fiyatı", "segment": "DEALER", "percent": "15"},
            {"label": "Toptan Fiyatı", "segment": "WHOLESALE", "percent": "15"},
        ],
        "retail": {"percent": "20", "vat_rate": "20"},
    }
    sheet.update(overrides)
    return sheet


# ── Arithmetic ───────────────────────────────────────────────────────────────


def test_each_column_builds_on_the_one_before():
    out = compute_sheet(PricingSheetIn.model_validate(_sheet()))
    assert [c["computed"] for c in out["columns"]] == ["1150.00", "1322.50"]
    # Retail is the after-VAT shelf price: the 1587 markup step, plus 20% VAT.
    assert out["retail"]["before_vat"] == "1587.00"
    assert out["retail"]["computed"] == out["retail"]["price"] == "1904.40"


def test_retail_on_an_after_vat_sheet_adds_no_more_vat():
    out = compute_sheet(PricingSheetIn.model_validate(_sheet(vat_mode="INCL")))
    assert out["retail"]["before_vat"] == out["retail"]["computed"] == "1587.00"


def test_a_typed_price_never_moves_the_calculated_chain():
    columns = [
        {"label": "Bayi Fiyatı", "segment": "DEALER", "percent": "15", "desired": "1200"},
        {"label": "Toptan Fiyatı", "segment": "WHOLESALE", "percent": "15", "desired": "1399.90"},
    ]
    out = compute_sheet(PricingSheetIn.model_validate(_sheet(columns=columns)))
    # The typed figures are the prices…
    assert [c["price"] for c in out["columns"]] == ["1200", "1399.90"]
    # …and the calculated row runs on as if nothing had been typed.
    assert [c["computed"] for c in out["columns"]] == ["1150.00", "1322.50"]
    assert out["retail"]["computed"] == out["retail"]["price"] == "1904.40"


def test_a_foreign_sheet_gives_the_tl_figure_at_the_typed_rate():
    out = compute_sheet(PricingSheetIn.model_validate(_sheet(
        currency="USD", cost="10", columns=[], retail={"percent": "100", "vat_rate": "20", "desired": "25"},
        usd_rate="41.50", eur_rate="48.10",
    )))
    # 10 + 100% = 20, + 20% VAT = 24; the typed 25 is the shelf price.
    assert (out["retail"]["computed"], out["retail"]["price"], out["try_price"]) == ("24.00", "25", "1037.50")


def test_an_extra_sits_before_retail_and_retail_builds_on_it_before_vat():
    extras = [{"label": "Özel Fiyat", "percent": "10", "desired": "1800"}]
    out = compute_sheet(PricingSheetIn.model_validate(_sheet(extras=extras)))
    # Toptan 1322.50; +10% = 1454.75, + 20% VAT = 1745.70 on show; its typed 1800 is its price.
    assert [(e["before_vat"], e["computed"], e["price"]) for e in out["extras"]] == [("1454.75", "1745.70", "1800")]
    # Retail: 1454.75 + 20% = 1745.70 before VAT (the chain, not the typed 1800), + 20% VAT = 2094.84.
    assert (out["retail"]["before_vat"], out["retail"]["computed"]) == ("1745.70", "2094.84")
    # Without an extra, retail builds on the last class as it always has.
    plain = compute_sheet(PricingSheetIn.model_validate(_sheet()))
    assert plain["extras"] == [] and plain["retail"]["computed"] == "1904.40"
    inclusive = compute_sheet(PricingSheetIn.model_validate(_sheet(vat_mode="INCL", extras=extras)))
    assert inclusive["extras"][0]["computed"] == inclusive["extras"][0]["before_vat"] == "1454.75"


def test_the_applied_percent_goes_on_each_classs_erp_price_else_on_the_calculated_one():
    columns = [
        {"label": "Bayi Fiyatı", "segment": "DEALER", "percent": "15", "erp_price": "1200"},
        {"label": "Toptan Fiyatı", "segment": "WHOLESALE", "percent": "15"},
    ]
    extras = [{"label": "Mağaza Fiyatı", "percent": "10"}]
    retail = {"percent": "20", "vat_rate": "20", "erp_price": "2000"}
    sheet = _sheet(columns=columns, extras=extras, retail=retail, applied_percent="5")
    out = compute_sheet(PricingSheetIn.model_validate(sheet))
    assert out["applied_percent"] == "5"
    # Bayi's typed ERP 1200 + 5% = 1260; Toptan has none: its calculated 1322.50 + 5% = 1388.625, half a kuruş up.
    assert [(c["computed"], c["erp_price"], c["price"]) for c in out["columns"]] == [
        ("1150.00", "1200", "1260.00"),
        ("1322.50", None, "1388.63"),
    ]
    # Mağaza Fiyatı and retail never take the rate: the calculated 1745.70, and
    # retail's ERP 2000, as they are.
    assert (out["extras"][0]["price"], out["retail"]["price"]) == ("1745.70", "2000.00")
    # The chain runs on calculated values only: retail is still 2094.84.
    assert out["retail"]["computed"] == "2094.84"
    # A typed uygulanacak fiyat still wins over all of it.
    typed = compute_sheet(PricingSheetIn.model_validate({**sheet, "retail": {**retail, "desired": "2222"}}))
    assert typed["retail"]["price"] == "2222"
    # Without a rate an ERP fiyatı is the price as it is.
    plain = compute_sheet(PricingSheetIn.model_validate(_sheet(columns=columns)))
    assert plain["columns"][0]["price"] == "1200.00" and plain["applied_percent"] is None
    # Rows from before (no ERP fields, or the rates of earlier versions) read as before.
    old = compute_sheet(PricingSheetIn.model_validate(_sheet(
        erp_percent="5", columns=[{"label": "Bayi", "segment": "DEALER", "percent": "15", "erp_percent": "5"}],
    )))
    assert old["columns"][0]["price"] == "1150.00" and old["retail"]["price"] == "1656.00"



def test_on_cetvel_2_the_erp_price_takes_vat_before_it_is_a_base():
    """The blue row is before VAT on every sheet; Cetvel 2's green row is after
    it, so 485 at 20% VAT is 582 there (then the rate), and retail's 595 is 714."""
    columns = [
        {"label": "Bayi Fiyatı", "segment": "DEALER", "percent": "15", "erp_price": "485"},
        {"label": "Toptan Fiyatı", "segment": "WHOLESALE", "percent": "0", "erp_price": "485"},
        {"label": "Kafe Fiyatı", "segment": "CAFE", "percent": "10", "erp_price": "530"},
        {"label": "Ara", "segment": None, "percent": "0"},
    ]
    extras = [{"label": "Mağaza Fiyatı", "percent": "10", "erp_price": "600"}]
    retail = {"percent": "10", "vat_rate": "20", "erp_price": "595"}
    sheet = _sheet(vat_mode="INCL", cost="400", columns=columns, extras=extras, retail=retail)
    out = compute_sheet(PricingSheetIn.model_validate(sheet))
    assert [c["price"] for c in out["columns"]] == ["582.00", "582.00", "636.00", "506.00"]
    assert (out["extras"][0]["price"], out["retail"]["price"]) == ("720.00", "714.00")
    # The typed ERP fiyatı is kept as typed, and the calculated chain is untouched.
    assert out["retail"]["erp_price"] == "595" and out["columns"][0]["computed"] == "460.00"
    # The uygulanacak oran goes on after VAT: 582 + 5% = 611.10, 636 + 5% = 667.80.
    rated = compute_sheet(PricingSheetIn.model_validate({**sheet, "applied_percent": "5"}))
    assert [c["price"] for c in rated["columns"][:3]] == ["611.10", "611.10", "667.80"]
    assert rated["retail"]["price"] == "714.00"  # retail never takes the rate
    # Cetvel 1 is before VAT already: the ERP fiyatı is the base as it is.
    excl = compute_sheet(PricingSheetIn.model_validate({**sheet, "vat_mode": "EXCL"}))
    assert [c["price"] for c in excl["columns"][:3]] == ["485.00", "485.00", "530.00"]

@pytest.mark.parametrize(
    "change",
    [
        {"cost": "0"},
        {"columns": [{"label": "Ara", "segment": "RETAIL", "percent": "10"}]},
        {"columns": [{"label": "A", "segment": "CAFE", "percent": "1"}, {"label": "B", "segment": "CAFE", "percent": "1"}]},
        {"retail": {"percent": "10", "vat_rate": "120"}},
        {"extras": [{"label": "", "percent": "10"}]},
        {"extras": [{"label": "Özel", "percent": "10"}] * 7},
    ],
)
async def test_impossible_sheets_are_refused(client: AsyncClient, admin_user: User, change: dict):
    resp = await client.post(f"{BASE}/draft", headers=auth_header(admin_user), json={"sheet": _sheet(**change)})
    assert resp.status_code == 422, resp.text


# ── Drafts and files ─────────────────────────────────────────────────────────


async def test_drafts_and_files(client: AsyncClient, admin_user: User):
    h = auth_header(admin_user)
    draft = await client.post(f"{BASE}/draft", headers=h, json={"sheet": _sheet(product_name="Taslak ürün")})
    assert draft.status_code == 201, draft.text
    assert draft.json()["file_id"] is None and draft.json()["sheet"]["retail"]["price"] == "1904.40"
    drafts = (await client.get(f"{BASE}/records", headers=h)).json()
    assert draft.json()["id"] in [r["id"] for r in drafts]

    first = await client.post(f"{BASE}/file", headers=h, json={"file_name": "Ekim 2026 Zam", "sheet": _sheet()})
    assert first.status_code == 201, first.text
    file_id = first.json()["file_id"]
    # The same name (any case) is the same file, not a second one.
    again = await client.post(f"{BASE}/file", headers=h, json={"file_name": "ekim 2026 zam", "sheet": _sheet()})
    assert again.json()["file_id"] == file_id
    by_id = await client.post(f"{BASE}/file", headers=h, json={"file_id": file_id, "sheet": _sheet()})
    assert by_id.json()["file_name"] == "Ekim 2026 Zam"

    files = {f["id"]: f for f in (await client.get(f"{BASE}/files", headers=h)).json()}
    assert files[file_id]["record_count"] == 3
    # Saving into it moved its "updated" time past its creation.
    assert files[file_id]["updated_at"] > files[file_id]["created_at"]
    rows = (await client.get(f"{BASE}/records", headers=h, params={"file_id": file_id})).json()
    assert len(rows) == 3 and draft.json()["id"] not in [r["id"] for r in rows]

    assert (await client.delete(f"{BASE}/records/{rows[0]['id']}", headers=h)).status_code == 204
    assert (await client.delete(f"{BASE}/files/{file_id}", headers=h)).status_code == 204
    assert (await client.get(f"{BASE}/records", headers=h, params={"file_id": file_id})).json() == []

    neither = await client.post(f"{BASE}/file", headers=h, json={"sheet": _sheet()})
    assert neither.status_code == 422


async def test_a_saved_row_is_overwritten_in_place(client: AsyncClient, admin_user: User):
    h = auth_header(admin_user)
    code = f"OVR-{uuid.uuid4().hex[:6]}"
    first = (await client.post(f"{BASE}/draft", headers=h, json={"sheet": _sheet(product_code=code, cost="1000")})).json()
    other = (await client.post(f"{BASE}/draft", headers=h, json={"sheet": _sheet(product_name="Başka")})).json()

    # The drafts hold the code, found whatever its case and spaces; a file does not.
    found = (await client.get(f"{BASE}/records/matching", headers=h, params={"code": f"  {code.lower()} "})).json()
    assert [r["id"] for r in found] == [first["id"]]
    assert (await client.get(f"{BASE}/records/matching", headers=h, params={"code": code, "file_name": "Yok böyle"})).json() == []
    assert (await client.get(f"{BASE}/records/matching", headers=h, params={"code": " "})).json() == []

    # "Üzerine Yaz" in the drafts: the same row, its sheet and its time new, nothing added.
    drafts_before = len((await client.get(f"{BASE}/records", headers=h)).json())
    over = await client.post(f"{BASE}/draft", headers=h, json={"sheet": _sheet(product_code=code, cost="1200"), "replace_id": first["id"]})
    assert over.status_code == 201, over.text
    over = over.json()
    assert over["id"] == first["id"] and over["sheet"]["cost"] == "1200"
    assert over["created_at"] == first["created_at"] and over["updated_at"] > first["updated_at"]
    drafts = (await client.get(f"{BASE}/records", headers=h)).json()
    assert len(drafts) == drafts_before
    # Newest save first: the overwritten row leads, ahead of one made after it.
    assert [r["id"] for r in drafts].index(first["id"]) < [r["id"] for r in drafts].index(other["id"])

    # Into a file: a row is overwritten there and the file's count stays.
    saved = (await client.post(f"{BASE}/file", headers=h, json={"file_name": f"Dosya {code}", "sheet": _sheet(product_code=code)})).json()
    file_id = saved["file_id"]
    by_name = (await client.get(f"{BASE}/records/matching", headers=h, params={"code": code, "file_name": f"dosya {code.lower()}"})).json()
    assert [r["id"] for r in by_name] == [saved["id"]]
    again = await client.post(
        f"{BASE}/file", headers=h, json={"file_id": file_id, "sheet": _sheet(product_code=code, cost="1500"), "replace_id": saved["id"]}
    )
    assert again.json()["id"] == saved["id"] and again.json()["sheet"]["cost"] == "1500"
    files = {f["id"]: f for f in (await client.get(f"{BASE}/files", headers=h)).json()}
    assert files[file_id]["record_count"] == 1

    # A draft saved over into a file moves there.
    moved = (await client.post(f"{BASE}/file", headers=h, json={"file_id": file_id, "sheet": _sheet(product_code=code), "replace_id": first["id"]})).json()
    assert moved["id"] == first["id"] and moved["file_id"] == file_id
    assert first["id"] not in [r["id"] for r in (await client.get(f"{BASE}/records", headers=h)).json()]

    # A row deleted meanwhile cannot be written over.
    gone = await client.post(f"{BASE}/draft", headers=h, json={"sheet": _sheet(), "replace_id": str(uuid.uuid4())})
    assert gone.status_code == 404

    # A copied file keeps its rows' times.
    copy = (await client.post(f"{BASE}/files/{file_id}/duplicate", headers=h, json={})).json()
    times = lambda rows: sorted((r["product_code"], r["updated_at"]) for r in rows)  # noqa: E731
    assert times(await _rows(client, h, copy["id"])) == times(await _rows(client, h, file_id))
    for f in (file_id, copy["id"]):
        await client.delete(f"{BASE}/files/{f}", headers=h)
    await client.delete(f"{BASE}/records/{other['id']}", headers=h)


async def _file_with(client: AsyncClient, h: dict, name: str, n: int) -> tuple[int, list[str]]:
    ids = []
    for i in range(n):
        resp = await client.post(f"{BASE}/file", headers=h, json={"file_name": name, "sheet": _sheet(product_name=f"{name} {i}")})
        assert resp.status_code == 201, resp.text
        ids.append(resp.json()["id"])
    return resp.json()["file_id"], ids


async def _rows(client: AsyncClient, h: dict, file_id: int) -> list[dict]:
    return (await client.get(f"{BASE}/records", headers=h, params={"file_id": file_id})).json()


async def test_deleting_picked_rows_never_deletes_their_file(client: AsyncClient, admin_user: User):
    h = auth_header(admin_user)
    file_id, ids = await _file_with(client, h, "Silme Testi", 4)
    resp = await client.post(f"{BASE}/records/delete", headers=h, json={"ids": ids[:2]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 2
    left = await _rows(client, h, file_id)
    assert sorted(r["id"] for r in left) == sorted(ids[2:])
    files = {f["id"]: f for f in (await client.get(f"{BASE}/files", headers=h)).json()}
    assert files[file_id]["record_count"] == 2
    assert (await client.post(f"{BASE}/records/delete", headers=h, json={"ids": []})).status_code == 422


async def test_picked_rows_move_to_another_file_or_a_new_one(client: AsyncClient, admin_user: User):
    h = auth_header(admin_user)
    source, ids = await _file_with(client, h, "Taşıma Kaynak", 3)
    target, _ = await _file_with(client, h, "Taşıma Hedef", 1)
    resp = await client.post(f"{BASE}/records/move", headers=h, json={"ids": ids[:2], "file_id": target})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["count"], resp.json()["file"]["id"], resp.json()["file"]["record_count"]) == (2, target, 3)
    assert [r["id"] for r in await _rows(client, h, source)] == [ids[2]]
    # To a new file, by its name.
    new = await client.post(f"{BASE}/records/move", headers=h, json={"ids": [ids[2]], "file_name": "Taşıma Yeni"})
    assert new.status_code == 200, new.text
    assert new.json()["file"]["name"] == "Taşıma Yeni" and new.json()["file"]["record_count"] == 1
    assert await _rows(client, h, source) == []
    missing = await client.post(f"{BASE}/records/move", headers=h, json={"ids": [ids[0]], "file_id": 999999})
    assert missing.status_code == 404


async def test_a_file_is_renamed_but_never_to_a_taken_name(client: AsyncClient, admin_user: User):
    h = auth_header(admin_user)
    file_id, _ = await _file_with(client, h, "Ad Eski", 1)
    await _file_with(client, h, "Ad Dolu", 1)
    resp = await client.patch(f"{BASE}/files/{file_id}", headers=h, json={"name": "  Ad   Yeni "})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Ad Yeni"
    taken = await client.patch(f"{BASE}/files/{file_id}", headers=h, json={"name": "ad dolu"})
    assert taken.status_code == 409
    # Its own name again (another case) is fine.
    assert (await client.patch(f"{BASE}/files/{file_id}", headers=h, json={"name": "AD YENİ"})).status_code == 200
    assert (await client.patch(f"{BASE}/files/{file_id}", headers=h, json={"name": "   "})).status_code == 422


async def test_a_duplicated_file_copies_every_row_as_new_unapplied_rows(
    client: AsyncClient, db_session: AsyncSession, admin_user: User
):
    h = auth_header(admin_user)
    file_id, ids = await _file_with(client, h, "Senaryo A", 3)
    first = await client.post(f"{BASE}/files/{file_id}/duplicate", headers=h, json={})
    assert first.status_code == 201, first.text
    assert (first.json()["name"], first.json()["record_count"]) == ("Senaryo A (kopya)", 3)
    second = await client.post(f"{BASE}/files/{file_id}/duplicate", headers=h, json={})
    assert second.json()["name"] == "Senaryo A (kopya 2)"
    named = await client.post(f"{BASE}/files/{file_id}/duplicate", headers=h, json={"name": "Senaryo B"})
    assert named.json()["name"] == "Senaryo B"
    assert (await client.post(f"{BASE}/files/{file_id}/duplicate", headers=h, json={"name": "senaryo b"})).status_code == 409

    original = await _rows(client, h, file_id)
    copies = await _rows(client, h, first.json()["id"])
    assert [r["product_name"] for r in copies] == [r["product_name"] for r in original]  # same rows, same order
    assert [r["sheet"] for r in copies] == [r["sheet"] for r in original]
    assert not {r["id"] for r in copies} & set(ids) and all(r["applied_at"] is None for r in copies)
    # The copy is its own: changing it leaves the original alone.
    await client.post(f"{BASE}/records/delete", headers=h, json={"ids": [copies[0]["id"]]})
    assert len(await _rows(client, h, file_id)) == 3


async def test_only_admins_reach_the_calculator(client: AsyncClient, customer_user: User):
    h = auth_header(customer_user)
    assert (await client.post(f"{BASE}/draft", headers=h, json={"sheet": _sheet()})).status_code == 403
    assert (await client.get(f"{BASE}/files", headers=h)).status_code == 403


# ── Applying to the product ──────────────────────────────────────────────────


async def _prices(db: AsyncSession, variant_id) -> dict[SegmentType, list[VariantPrice]]:
    codes = dict((await db.execute(select(CustomerSegment.id, CustomerSegment.code))).all())
    rows = (await db.execute(
        select(VariantPrice).where(VariantPrice.variant_id == variant_id).execution_options(populate_existing=True)
    )).scalars().all()
    out: dict[SegmentType, list[VariantPrice]] = {}
    for r in rows:
        out.setdefault(SegmentType(codes[r.segment_id]), []).append(r)
    return out


async def _code(db: AsyncSession, variant: ProductVariant) -> str:
    return await db.scalar(select(Product.base_code).where(Product.id == variant.product_id))


async def test_apply_writes_every_customer_groups_live_price(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, catalog
):
    normal_v, _ = catalog
    code = await _code(db_session, normal_v)
    columns = [
        {"label": "Bayi Fiyatı", "segment": "DEALER", "percent": "15"},
        {"label": "Ara Hesap", "segment": None, "percent": "0"},
        {"label": "Toptan Fiyatı", "segment": "WHOLESALE", "percent": "15"},
        {"label": "Kafe Fiyatı", "segment": "CAFE", "percent": "0", "desired": "1350"},
    ]
    resp = await client.post(
        f"{BASE}/apply-to-product", headers=auth_header(admin_user),
        json={
            "file_name": "Uygulanan",
            "sheet": _sheet(product_code=code.lower(), columns=columns, extras=[{"label": "Özel Fiyat 1", "percent": "5"}]),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # An extra is for reference only: reported, never written.
    assert body["variants_updated"] == 1 and body["skipped"] == ["Ara Hesap", "Özel Fiyat 1"]
    assert body["record"]["applied_at"] is not None

    prices = await _prices(db_session, normal_v.id)
    got = {seg: (rows[0].currency, rows[0].price_excl_tax, rows[0].price_incl_tax) for seg, rows in prices.items()}
    # VAT-excluded sheet at %20: the figure is the excluded side.
    assert got[SegmentType.DEALER] == ("TRY", Decimal("1150.00"), Decimal("1380.00"))
    assert got[SegmentType.WHOLESALE] == ("TRY", Decimal("1322.50"), Decimal("1587.00"))
    assert got[SegmentType.CAFE] == ("TRY", Decimal("1350.00"), Decimal("1620.00"))
    # Retail runs on the calculated chain (1322.50 through the 0% columns, then
    # Özel Fiyat's +5% = 1388.625) + 20%, not on Kafe's typed 1350: 1666.35,
    # plus 20% VAT 1999.62 on the shelf.
    assert got[SegmentType.RETAIL] == ("TRY", Decimal("1666.35"), Decimal("1999.62"))
    assert all(len(rows) == 1 for rows in prices.values())


async def test_a_row_applied_then_saved_over_keeps_its_application(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, catalog
):
    normal_v, _ = catalog
    code = await _code(db_session, normal_v)
    h = auth_header(admin_user)
    applied = (await client.post(f"{BASE}/apply-to-product", headers=h, json={"file_name": f"Uygula {code}", "sheet": _sheet(product_code=code)})).json()
    row = applied["record"]
    assert row["applied_at"] >= row["updated_at"]

    # Overwritten and applied again: the same row, applied after its save.
    again = await client.post(
        f"{BASE}/apply-to-product", headers=h,
        json={"file_id": row["file_id"], "sheet": _sheet(product_code=code, cost="1100"), "replace_id": row["id"]},
    )
    assert again.status_code == 200, again.text
    again = again.json()["record"]
    assert again["id"] == row["id"] and again["applied_at"] > row["applied_at"] and again["applied_at"] >= again["updated_at"]

    # Overwritten without applying: the application stays, older than the save.
    plain = (await client.post(f"{BASE}/file", headers=h, json={"file_id": row["file_id"], "sheet": _sheet(product_code=code, cost="1300"), "replace_id": row["id"]})).json()
    assert plain["applied_at"] == again["applied_at"] and plain["applied_summary"] is not None
    assert plain["updated_at"] > plain["applied_at"]
    await client.delete(f"{BASE}/files/{row['file_id']}", headers=h)


async def test_a_foreign_sheet_replaces_try_rows_and_retail_takes_the_final_decision(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, catalog
):
    normal_v, _ = catalog  # has a TRY retail price from the fixture
    code = await _code(db_session, normal_v)
    sheet = _sheet(
        product_code=code, vat_mode="INCL", currency="USD", cost="100",
        columns=[{"label": "Bayi Fiyatı", "segment": "DEALER", "percent": "10"}],
        retail={"percent": "50", "vat_rate": "20"}, usd_rate="40", final_try="6999.90",
    )
    resp = await client.post(f"{BASE}/apply-to-product", headers=auth_header(admin_user), json={"file_name": "USD", "sheet": sheet})
    assert resp.status_code == 200, resp.text

    prices = await _prices(db_session, normal_v.id)
    dealer = prices[SegmentType.DEALER]
    assert [(r.currency, r.price_incl_tax, r.price_excl_tax) for r in dealer] == [("USD", Decimal("110.00"), Decimal("91.67"))]
    retail = prices[SegmentType.RETAIL]
    assert [(r.currency, r.price_incl_tax) for r in retail] == [("TRY", Decimal("6999.90"))]

    # And the same sheet without a final decision prices retail in USD, the TRY row gone.
    sheet.pop("final_try")
    await client.post(f"{BASE}/apply-to-product", headers=auth_header(admin_user), json={"file_name": "USD", "sheet": sheet})
    retail = (await _prices(db_session, normal_v.id))[SegmentType.RETAIL]
    assert [(r.currency, r.price_incl_tax) for r in retail] == [("USD", Decimal("165.00"))]


async def test_an_unknown_product_saves_nothing(client: AsyncClient, db_session: AsyncSession, admin_user: User):
    h = auth_header(admin_user)  # before the request: its rollback expires the shared session's objects
    before = await db_session.scalar(select(func.count()).select_from(PricingRecord))
    resp = await client.post(
        f"{BASE}/apply-to-product", headers=h,
        json={"file_name": "Hiç olmayan", "sheet": _sheet(product_code="YOK-123")},
    )
    assert resp.status_code == 422 and "YOK-123" in resp.json()["detail"]
    assert await db_session.scalar(select(func.count()).select_from(PricingRecord)) == before
    files = (await client.get(f"{BASE}/files", headers=h)).json()
    assert "Hiç olmayan" not in [f["name"] for f in files]


async def test_rows_carry_their_products_photo(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, catalog
):
    """Looked up by code (any case) when read, so a changed photo shows at once."""
    normal_v, _ = catalog
    h = auth_header(admin_user)
    product = await db_session.get(Product, normal_v.product_id)
    product.image_url = "https://cdn.example.com/urun.jpg"
    code = product.base_code
    await db_session.commit()

    saved = await client.post(f"{BASE}/draft", headers=h, json={"sheet": _sheet(product_code=code.lower())})
    assert saved.json()["image_url"] == "https://cdn.example.com/urun.jpg"
    unknown = await client.post(f"{BASE}/draft", headers=h, json={"sheet": _sheet(product_code="YOK-1")})
    assert unknown.json()["image_url"] is None

    rows = {r["id"]: r for r in (await client.get(f"{BASE}/records", headers=h)).json()}
    assert rows[saved.json()["id"]]["image_url"] == "https://cdn.example.com/urun.jpg"
    assert rows[unknown.json()["id"]]["image_url"] is None
