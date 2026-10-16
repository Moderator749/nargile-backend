"""Fiyat Hesaplama Cetveli.

Covers:
- The sequential arithmetic: 1000 → +15% → 1150 → +15% → 1322.50 → +20% → 1587,
  and retail after VAT on top of it (1587 + %20 KDV = 1904.40)
- A typed "uygulanacak fiyat" is the column's price but never moves the calculated chain
- USD/EUR sheets: the TL figure at the typed rate
- Drafts and files: saving, reusing a file by name, listing, deleting
- Applying to a product: every customer group's live price, both VAT sides,
  other-currency rows removed, "son karar" as the TRY retail price
- A product that cannot be found saves nothing; admins only; bad sheets refused
"""

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


@pytest.mark.parametrize(
    "change",
    [
        {"cost": "0"},
        {"columns": [{"label": "Ara", "segment": "RETAIL", "percent": "10"}]},
        {"columns": [{"label": "A", "segment": "CAFE", "percent": "1"}, {"label": "B", "segment": "CAFE", "percent": "1"}]},
        {"retail": {"percent": "10", "vat_rate": "120"}},
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
        json={"file_name": "Uygulanan", "sheet": _sheet(product_code=code.lower(), columns=columns)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["variants_updated"] == 1 and body["skipped"] == ["Ara Hesap"]
    assert body["record"]["applied_at"] is not None

    prices = await _prices(db_session, normal_v.id)
    got = {seg: (rows[0].currency, rows[0].price_excl_tax, rows[0].price_incl_tax) for seg, rows in prices.items()}
    # VAT-excluded sheet at %20: the figure is the excluded side.
    assert got[SegmentType.DEALER] == ("TRY", Decimal("1150.00"), Decimal("1380.00"))
    assert got[SegmentType.WHOLESALE] == ("TRY", Decimal("1322.50"), Decimal("1587.00"))
    assert got[SegmentType.CAFE] == ("TRY", Decimal("1350.00"), Decimal("1620.00"))
    # Retail runs on the calculated chain (1322.50 through the 0% columns) + 20%,
    # not on Kafe's typed 1350; plus 20% VAT it is 1904.40 on the shelf.
    assert got[SegmentType.RETAIL] == ("TRY", Decimal("1587.00"), Decimal("1904.40"))
    assert all(len(rows) == 1 for rows in prices.values())


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
