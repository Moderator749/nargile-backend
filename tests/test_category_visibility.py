"""Category active switch and cover image, and the menu rules that lean on them."""

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Category
from app.models.content import SiteContent
from app.models.user import User
from tests.conftest import auth_header

PREFIX = "zz-test-"


@pytest.fixture
async def cleanup(db_session: AsyncSession):
    yield
    await db_session.rollback()
    await db_session.execute(delete(Category).where(Category.slug.like(f"{PREFIX}%"), Category.parent_id.is_not(None)))
    await db_session.execute(delete(Category).where(Category.slug.like(f"{PREFIX}%")))
    await db_session.execute(delete(SiteContent).where(SiteContent.key == "homepage"))
    await db_session.commit()


async def _create(client: AsyncClient, admin: User, **body) -> dict:
    response = await client.post("/api/v1/admin/categories", headers=auth_header(admin), json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def test_new_category_is_active_and_public(client: AsyncClient, admin_user: User, cleanup):
    created = await _create(
        client, admin_user, name="Elektronik Nargile", slug=f"{PREFIX}elektronik", image_url="/images/x.jpg"
    )
    assert created["is_active"] is True and created["image_url"] == "/images/x.jpg"
    public = {c["slug"]: c for c in (await client.get("/api/v1/categories/")).json()}
    assert public[f"{PREFIX}elektronik"]["image_url"] == "/images/x.jpg"


async def test_inactive_category_hides_its_branch_but_stays_in_admin(
    client: AsyncClient, admin_user: User, cleanup
):
    parent = await _create(client, admin_user, name="Ebeveyn", slug=f"{PREFIX}parent")
    await _create(client, admin_user, name="Çocuk", slug=f"{PREFIX}child", parent_id=parent["id"])

    off = await client.put(
        f"/api/v1/admin/categories/{parent['id']}",
        headers=auth_header(admin_user),
        json={"name": "Ebeveyn", "slug": f"{PREFIX}parent", "is_active": False},
    )
    assert off.status_code == 200 and off.json()["is_active"] is False

    public = {c["slug"] for c in (await client.get("/api/v1/categories/")).json()}
    assert f"{PREFIX}parent" not in public and f"{PREFIX}child" not in public
    admin = {c["slug"] for c in (await client.get("/api/v1/admin/categories", headers=auth_header(admin_user))).json()}
    assert {f"{PREFIX}parent", f"{PREFIX}child"} <= admin


async def test_category_image_must_be_a_url_or_site_path(client: AsyncClient, admin_user: User, cleanup):
    bad = await client.post(
        "/api/v1/admin/categories",
        headers=auth_header(admin_user),
        json={"name": "Kötü", "slug": f"{PREFIX}bad", "image_url": "javascript:alert(1)"},
    )
    assert bad.status_code == 422


async def test_menu_category_entries_without_a_label_and_retired_mode_key(
    client: AsyncClient, admin_user: User, cleanup
):
    # "sidebar_mode" is retired; documents saved with it still load, without it.
    doc = {
        "navigation": {
            "top": [{"label": "", "slug": "luleler"}],
            "sidebar_mode": "custom",
            "sidebar": [{"label": "LÜLELER", "href": "/catalog/luleler"}],
        }
    }
    saved = await client.put("/api/v1/admin/home/settings", headers=auth_header(admin_user), json=doc)
    assert saved.status_code == 200, saved.text
    navigation = saved.json()["data"]["navigation"]
    assert "sidebar_mode" not in navigation
    assert navigation["sidebar"][0]["href"] == "/catalog/luleler"

    # A plain link still needs words to show.
    nameless = {"navigation": {"top": [{"label": "", "href": "/catalog"}], "sidebar": []}}
    refused = await client.put("/api/v1/admin/home/settings", headers=auth_header(admin_user), json=nameless)
    assert refused.status_code == 422


async def test_clean_taxonomy_is_in_place(client: AsyncClient):
    """The migration's tree: seven groups, each with sub-categories, coal included."""
    rows = (await client.get("/api/v1/categories/")).json()
    groups = [c["slug"] for c in sorted((c for c in rows if c["parent_id"] is None), key=lambda c: c["sort_order"])]
    expected = [
        "nargile-takimlari", "luleler", "kozlukler-isi-yonetimi", "marpuc-ve-hortumlar",
        "sipsiler-ve-aksesuarlar", "nargile-komurleri", "tutun-bitkisel-ve-sarf",
    ]
    assert [g for g in groups if g in expected] == expected
    by_slug = {c["slug"]: c for c in rows}
    for group in expected:
        assert any(c["parent_id"] == by_slug[group]["id"] for c in rows), group
    # Nothing of the replaced tree survives.
    for gone in ("komurler", "tuketim-sarf", "donanim-parcalar", "rus-takimlar", "masaustu-takimlar", "aksesuarlar"):
        assert gone not in by_slug


async def test_coal_category_is_protected(client: AsyncClient, admin_user: User, db_session: AsyncSession):
    coal = await db_session.scalar(select(Category).where(Category.slug == "nargile-komurleri"))
    h = auth_header(admin_user)
    renamed = await client.put(
        f"/api/v1/admin/categories/{coal.id}", headers=h, json={"name": "Kömür", "slug": "komur", "parent_id": None}
    )
    assert renamed.status_code == 409
    assert (await client.delete(f"/api/v1/admin/categories/{coal.id}", headers=h)).status_code == 409
    # Its name and order can still change; only the address the rules use is fixed.
    ok = await client.put(
        f"/api/v1/admin/categories/{coal.id}",
        headers=h,
        json={"name": coal.name, "slug": "nargile-komurleri", "parent_id": None, "sort_order": coal.sort_order},
    )
    assert ok.status_code == 200, ok.text
