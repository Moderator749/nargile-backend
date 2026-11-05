""""Kaydet" and "Kaydet ve Siteye Uygula" (POST /admin/pricing/save): one list,
one rule.

Covers:
- A new code saves straight away, as a row in no folder
- A code already saved — as a draft or in any file — answers 409 with those
  rows; "Üstüne Yaz" writes over the chosen one (it stays in its folder),
  "Yeni Kayıt" adds one
- A sheet loaded from a row, its code unchanged, is saved over that row
  without a question, even when the code is saved elsewhere too
- The loaded row's code changed: asked about when the new code exists, a new
  row when it does not; the loaded row is left as it was
- No code: always a new row; applying needs a code
- "Kaydet ve Siteye Uygula": saved and the green row written to the product,
  all or nothing
- "Kayıtlar": every row in one list; matching across folders
"""

import uuid
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pricing import PricingRecord
from app.models.user import SegmentType, User
from tests.conftest import auth_header
from tests.test_pricing_sheet import BASE, _code, _prices, _sheet


def _new_code() -> str:
    return f"SV-{uuid.uuid4().hex[:6]}"


async def _rows_of(db: AsyncSession, code: str) -> int:
    return await db.scalar(select(func.count()).select_from(PricingRecord).where(PricingRecord.product_code == code))


async def test_a_new_code_saves_straight_away_in_no_folder(client: AsyncClient, db_session: AsyncSession, admin_user: User):
    code = _new_code()
    resp = await client.post(f"{BASE}/save", headers=auth_header(admin_user), json={"sheet": _sheet(product_code=code)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["overwritten"] is False and body["applied"] is None
    assert body["record"]["file_id"] is None and body["record"]["sheet"]["retail"]["price"] == "1904.40"
    assert await _rows_of(db_session, code) == 1


async def test_a_code_saved_anywhere_is_asked_about_then_overwritten_or_added(
    client: AsyncClient, db_session: AsyncSession, admin_user: User
):
    h = auth_header(admin_user)
    code = _new_code()
    # Saved before the change: once in a file, once as a draft.
    in_file = (await client.post(f"{BASE}/file", headers=h, json={"file_name": f"Ekim {code}", "sheet": _sheet(product_code=code)})).json()
    draft = (await client.post(f"{BASE}/draft", headers=h, json={"sheet": _sheet(product_code=code, cost="900")})).json()

    asked = await client.post(f"{BASE}/save", headers=h, json={"sheet": _sheet(product_code=f"  {code.lower()} ", cost="1200")})
    assert asked.status_code == 409, asked.text
    detail = asked.json()["detail"]
    assert code in detail["message"]
    # Both, newest first, wherever they are.
    assert [m["id"] for m in detail["matches"]] == [draft["id"], in_file["id"]]
    assert detail["matches"][1]["file_name"] == f"Ekim {code}"
    assert await _rows_of(db_session, code) == 2  # nothing saved by the question

    over = await client.post(
        f"{BASE}/save", headers=h,
        json={"sheet": _sheet(product_code=code, cost="1200"), "mode": "overwrite", "overwrite_id": in_file["id"]},
    )
    assert over.status_code == 200, over.text
    record = over.json()["record"]
    assert over.json()["overwritten"] is True
    # The same row, still in its folder, with the new figures.
    assert (record["id"], record["file_id"], record["sheet"]["cost"]) == (in_file["id"], in_file["file_id"], "1200")

    added = await client.post(f"{BASE}/save", headers=h, json={"sheet": _sheet(product_code=code), "mode": "new"})
    assert added.status_code == 200 and added.json()["overwritten"] is False
    assert added.json()["record"]["file_id"] is None
    assert await _rows_of(db_session, code) == 3


async def test_a_loaded_row_is_saved_over_without_a_question(client: AsyncClient, db_session: AsyncSession, admin_user: User):
    h = auth_header(admin_user)
    code = _new_code()
    first = (await client.post(f"{BASE}/save", headers=h, json={"sheet": _sheet(product_code=code)})).json()["record"]
    # The code is saved elsewhere too: the loaded row is still the one saved over.
    await client.post(f"{BASE}/save", headers=h, json={"sheet": _sheet(product_code=code), "mode": "new"})

    again = await client.post(f"{BASE}/save", headers=h, json={"sheet": _sheet(product_code=code, cost="1500"), "record_id": first["id"]})
    assert again.status_code == 200, again.text
    assert again.json()["overwritten"] is True and again.json()["record"]["id"] == first["id"]
    assert again.json()["record"]["sheet"]["cost"] == "1500"
    assert await _rows_of(db_session, code) == 2


async def test_a_changed_code_is_a_new_row_or_a_question(client: AsyncClient, db_session: AsyncSession, admin_user: User):
    h = auth_header(admin_user)
    loaded = (await client.post(f"{BASE}/save", headers=h, json={"sheet": _sheet(product_code=_new_code())})).json()["record"]
    taken = (await client.post(f"{BASE}/save", headers=h, json={"sheet": _sheet(product_code=_new_code())})).json()["record"]

    # Changed to a code saved elsewhere: asked about, as a new sheet would be.
    asked = await client.post(
        f"{BASE}/save", headers=h, json={"sheet": _sheet(product_code=taken["product_code"]), "record_id": loaded["id"]}
    )
    assert asked.status_code == 409
    assert [m["id"] for m in asked.json()["detail"]["matches"]] == [taken["id"]]

    # Changed to a code saved nowhere: a new row; the loaded one stays as it was.
    fresh = _new_code()
    moved = await client.post(f"{BASE}/save", headers=h, json={"sheet": _sheet(product_code=fresh), "record_id": loaded["id"]})
    assert moved.status_code == 200 and moved.json()["overwritten"] is False
    assert moved.json()["record"]["id"] != loaded["id"]
    kept = await db_session.get(PricingRecord, uuid.UUID(loaded["id"]), populate_existing=True)
    assert kept.product_code == loaded["product_code"]


async def test_without_a_code_every_save_is_a_new_row(client: AsyncClient, admin_user: User):
    h = auth_header(admin_user)
    one = await client.post(f"{BASE}/save", headers=h, json={"sheet": _sheet(product_name="Kodsuz")})
    two = await client.post(f"{BASE}/save", headers=h, json={"sheet": _sheet(product_name="Kodsuz")})
    assert one.status_code == two.status_code == 200
    assert one.json()["record"]["id"] != two.json()["record"]["id"]
    # Applying needs a product to apply to.
    assert (await client.post(f"{BASE}/save", headers=h, json={"sheet": _sheet(), "apply": True})).status_code == 422
    # "overwrite" names its row, and only it does.
    assert (await client.post(f"{BASE}/save", headers=h, json={"sheet": _sheet(), "mode": "overwrite"})).status_code == 422
    gone = await client.post(f"{BASE}/save", headers=h, json={"sheet": _sheet(), "mode": "overwrite", "overwrite_id": str(uuid.uuid4())})
    assert gone.status_code == 404


async def test_save_and_apply_writes_the_green_row_to_the_product(
    client: AsyncClient, db_session: AsyncSession, admin_user: User, catalog
):
    h = auth_header(admin_user)
    normal_v, _ = catalog
    code = await _code(db_session, normal_v)
    resp = await client.post(f"{BASE}/save", headers=h, json={"sheet": _sheet(product_code=code), "apply": True, "mode": "new"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"]["variants_updated"] == 1 and body["record"]["applied_at"] is not None
    prices = await _prices(db_session, normal_v.id)
    assert prices[SegmentType.DEALER][0].price_excl_tax == Decimal("1150.00")
    assert prices[SegmentType.RETAIL][0].price_incl_tax == Decimal("1904.40")

    # The same code again from a new sheet: asked about before anything is written.
    asked = await client.post(f"{BASE}/save", headers=h, json={"sheet": _sheet(product_code=code, cost="2000"), "apply": True})
    assert asked.status_code == 409
    assert (await _prices(db_session, normal_v.id))[SegmentType.DEALER][0].price_excl_tax == Decimal("1150.00")


async def test_an_unknown_product_saves_nothing(client: AsyncClient, db_session: AsyncSession, admin_user: User):
    code = _new_code()
    resp = await client.post(f"{BASE}/save", headers=auth_header(admin_user), json={"sheet": _sheet(product_code=code), "apply": True})
    assert resp.status_code == 422
    assert await _rows_of(db_session, code) == 0


async def test_one_list_and_matching_across_folders(client: AsyncClient, admin_user: User):
    h = auth_header(admin_user)
    code = _new_code()
    in_file = (await client.post(f"{BASE}/file", headers=h, json={"file_name": f"Liste {code}", "sheet": _sheet(product_code=code)})).json()
    draft = (await client.post(f"{BASE}/draft", headers=h, json={"sheet": _sheet(product_code=code)})).json()
    listed = {r["id"] for r in (await client.get(f"{BASE}/records", headers=h, params={"all": "true"})).json()}
    assert {in_file["id"], draft["id"]} <= listed
    found = (await client.get(f"{BASE}/records/matching", headers=h, params={"code": code.lower(), "everywhere": "true"})).json()
    assert [r["id"] for r in found] == [draft["id"], in_file["id"]]
