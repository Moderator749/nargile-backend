"""The "Ana Sayfa Düzenleme" document and the hero slide layout fields."""

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content import HeroSlide, SiteContent
from app.models.user import User
from tests.conftest import auth_header

TAG = "⟪test⟫"


@pytest.fixture
async def cleanup(db_session: AsyncSession):
    yield
    await db_session.rollback()
    await db_session.execute(delete(SiteContent).where(SiteContent.key == "homepage"))
    await db_session.execute(delete(HeroSlide).where(HeroSlide.title.contains(TAG)))
    await db_session.commit()


DOC = {
    "sections": {"deals": False, "about": False},
    "deals": {"title": "Kaçırma", "subtitle": "Stoklarla sınırlı."},
    "brands": {"items": [" Oduman ", "Legend"]},
    "navigation": {
        "top": [{"label": "KAMPANYALAR", "href": "/#firsat-urunleri", "accent": True}],
        "sidebar": [{"label": "LÜLELER", "slug": "luleler"}],
    },
}


async def test_empty_until_saved_then_round_trips(client: AsyncClient, admin_user: User, cleanup):
    empty = (await client.get("/api/v1/home/settings")).json()
    assert empty["data"] == {}

    saved = await client.put("/api/v1/admin/home/settings", headers=auth_header(admin_user), json=DOC)
    assert saved.status_code == 200, saved.text

    public = (await client.get("/api/v1/home/settings")).json()["data"]
    # Sections the admin never touched are absent, so the storefront keeps its defaults.
    assert set(public) == {"sections", "deals", "brands", "navigation"}
    assert public["sections"]["deals"] is False and public["sections"]["hero"] is True
    assert public["brands"]["items"] == ["Oduman", "Legend"]
    assert public["navigation"]["top"][0]["accent"] is True


async def test_saving_requires_an_admin(client: AsyncClient, customer_user: User, cleanup):
    assert (await client.put("/api/v1/admin/home/settings", json=DOC)).status_code == 401
    forbidden = await client.put("/api/v1/admin/home/settings", headers=auth_header(customer_user), json=DOC)
    assert forbidden.status_code == 403


@pytest.mark.parametrize(
    "bad",
    [
        {"navigation": {"top": [{"label": "X", "href": "javascript:alert(1)"}], "sidebar": []}},
        {"deals": {"title": "", "subtitle": ""}},
        {"brands": {"items": ["x" * 41]}},
        {"sections": {"nope": True}},
    ],
)
async def test_rejects_unsafe_or_malformed_content(client: AsyncClient, admin_user: User, cleanup, bad: dict):
    response = await client.put("/api/v1/admin/home/settings", headers=auth_header(admin_user), json=bad)
    assert response.status_code == 422


async def test_hero_slide_layout_fields(client: AsyncClient, admin_user: User, cleanup):
    h = auth_header(admin_user)
    slide = {"title": f"Slayt {TAG}", "cta_label": "Git", "cta_href": "/catalog"}
    created = (await client.post("/api/v1/admin/hero-slides", headers=h, json=slide)).json()
    assert (created["text_align"], created["image_focus_x"], created["image_zoom"]) == ("center", 50, 100)

    updated = await client.put(
        f"/api/v1/admin/hero-slides/{created['id']}",
        headers=h,
        json={**slide, "text_align": "left", "image_focus_x": 20, "image_focus_y": 80, "image_zoom": 150},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert (body["text_align"], body["image_focus_x"], body["image_focus_y"], body["image_zoom"]) == ("left", 20, 80, 150)

    too_far = await client.put(
        f"/api/v1/admin/hero-slides/{created['id']}", headers=h, json={**slide, "image_zoom": 400}
    )
    assert too_far.status_code == 422
