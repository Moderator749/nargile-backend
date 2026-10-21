"""English and Arabic for products and categories: the admin writes them, the
storefront reads them, the import script loads them."""

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Category, Product
from app.models.user import User
from tests.conftest import auth_header
from tests.test_product_sheet import _post, _sheet

EN = {
    "name": "Diamond Cut Stainless Steel Hookah Set",
    "description": "<p>Hand-polished <strong>stainless steel</strong>.</p><script>alert(1)</script>",
    "meta_title": "Diamond Cut Steel Hookah Set",
    "meta_description": "A diamond-cut stainless steel hookah set, genuine and shipped from Istanbul, at wholesale prices for cafés and dealers.",
}
AR = {
    "name": "شيشة ستانلس ستيل كاملة بقطع الماس",
    "description": "<p>ستانلس ستيل مصقول يدويًا.</p>",
    "meta_title": "شيشة ستانلس ستيل بقطع الماس",
    "meta_description": "شيشة ستانلس ستيل كاملة بقطع الماس، منتج أصلي يُشحن من إسطنبول، بأسعار الجملة للمقاهي والموزعين.",
}


async def test_the_product_sheet_saves_translations_and_the_storefront_reads_them(
    client: AsyncClient, admin_user: User
):
    created = await _post(client, admin_user, _sheet(translations={"en": EN, "ar": AR}, meta_title="Elmas Kesim Nargile Takımı"))
    assert created.status_code == 201, created.text
    product = created.json()
    stored_en = product["translations"]["en"]
    # The same sanitizer as the Turkish description: the script is gone.
    assert stored_en["description"] == "<p>Hand-polished <strong>stainless steel</strong>.</p>"
    assert product["translations"]["ar"]["name"] == AR["name"]

    # The storefront asks in its language and gets the text in it.
    turkish = (await client.get(f"/api/v1/products/{product['slug']}")).json()
    english = (await client.get(f"/api/v1/products/{product['slug']}", params={"lang": "en"})).json()
    arabic = (await client.get(f"/api/v1/products/{product['slug']}", params={"lang": "ar"})).json()
    assert turkish["name"] == "Elmas Kesim Nargile" and turkish["locales"] == ["tr", "en", "ar"]
    assert (english["name"], english["meta_title"]) == (EN["name"], EN["meta_title"])
    assert english["description"] == stored_en["description"]
    assert arabic["meta_description"] == AR["meta_description"]
    # Prices are the same in every language.
    assert english["variants"][0]["price"] == turkish["variants"][0]["price"]

    # Lists too, and English search finds it by its English name.
    found = (await client.get("/api/v1/products", params={"q": "diamond cut", "lang": "en"})).json()
    assert [p["name"] for p in found if p["id"] == product["id"]] == [EN["name"]]
    assert not [p for p in (await client.get("/api/v1/products", params={"q": "diamond cut"})).json() if p["id"] == product["id"]]

    # Saved again by a form that does not send them: they stay.
    body = _sheet(base_code=product["base_code"])
    kept = await client.put(f"/api/v1/admin/products/{product['id']}/sheet", headers=auth_header(admin_user), json=body)
    assert kept.status_code == 200, kept.text
    assert kept.json()["translations"]["en"]["name"] == EN["name"]


async def test_an_untranslated_product_answers_in_turkish_without_its_turkish_seo(
    client: AsyncClient, admin_user: User
):
    product = (await _post(client, admin_user, _sheet(meta_title="Türkçe Başlık"))).json()
    english = (await client.get(f"/api/v1/products/{product['slug']}", params={"lang": "en"})).json()
    # Nothing to swap in: the Turkish stays, and the page knows it has no English.
    assert english["locales"] == ["tr"] and english["name"] == "Elmas Kesim Nargile"
    assert english["meta_title"] == "Türkçe Başlık"


@pytest.mark.parametrize(
    "translations",
    [
        {"fr": EN},  # not a storefront language
        {"en": {**EN, "name": "  "}},
        {"en": {**EN, "meta_title": "x" * 71}},
        {"ar": {**AR, "meta_description": "x" * 161}},
    ],
)
async def test_translations_are_held_to_their_limits(client: AsyncClient, admin_user: User, translations: dict):
    assert (await _post(client, admin_user, _sheet(translations=translations))).status_code == 422


async def test_a_category_takes_translations(client: AsyncClient, admin_user: User, db_session: AsyncSession):
    h = auth_header(admin_user)
    slug = f"ceviri-{uuid.uuid4().hex[:6]}"
    created = await client.post("/api/v1/admin/categories", headers=h, json={"name": "Közlükler Test", "slug": slug})
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    body = {
        "name": "Közlükler Test",
        "slug": slug,
        "translations": {
            "en": {"name": "Heat Management Devices", "description": "<p>Keep the coal at the right heat.</p>"},
            "ar": {"name": "منظمات حرارة الشيشة"},
        },
    }
    saved = await client.put(f"/api/v1/admin/categories/{cid}", headers=h, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["translations"]["ar"] == {"name": "منظمات حرارة الشيشة", "description": None}

    turkish = next(c for c in (await client.get("/api/v1/categories")).json() if c["id"] == cid)
    english = next(c for c in (await client.get("/api/v1/categories", params={"lang": "en"})).json() if c["id"] == cid)
    assert (turkish["name"], turkish["locales"]) == ("Közlükler Test", ["tr", "en", "ar"])
    assert (english["name"], english["description"]) == ("Heat Management Devices", "<p>Keep the coal at the right heat.</p>")
    arabic = (await client.get(f"/api/v1/categories/by-slug/{slug}", params={"lang": "ar"})).json()
    assert arabic["name"] == "منظمات حرارة الشيشة"
    await db_session.delete(await db_session.get(Category, cid))
    await db_session.commit()


async def test_the_import_script_merges_validated_translations(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, tmp_path
):
    from scripts.import_translations import run

    product = (await _post(client, admin_user, _sheet(translations={"ar": AR}))).json()
    unknown = str(uuid.uuid4())
    path = tmp_path / "translations.json"
    path.write_text(json.dumps([{"id": product["id"], "en": EN}, {"id": unknown, "en": EN}], ensure_ascii=False))

    dry = await run([path], None, commit=False)
    assert dry["products"]["updated"] == 1 and dry["products"]["unknown"] == [unknown]
    row = await db_session.scalar(select(Product).where(Product.id == uuid.UUID(product["id"])))
    assert "en" not in (row.translations or {})  # a dry run writes nothing

    done = await run([path], None, commit=True)
    assert done["products"]["updated"] == 1
    await db_session.refresh(row)
    # English added, the Arabic it already had kept.
    assert row.translations["en"]["name"] == EN["name"] and row.translations["ar"]["name"] == AR["name"]
    again = await run([path], None, commit=True)
    assert (again["products"]["updated"], again["products"]["unchanged"]) == (0, 1)
