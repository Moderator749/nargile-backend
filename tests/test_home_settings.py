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


async def test_menu_entries_can_be_switched_off(client: AsyncClient, admin_user: User, cleanup):
    doc = {
        "navigation": {
            "top": [{"label": "LÜLELER", "slug": "luleler", "enabled": False}, {"label": "KÖMÜR", "slug": "komurler"}],
            "sidebar": [],
        }
    }
    saved = await client.put("/api/v1/admin/home/settings", headers=auth_header(admin_user), json=doc)
    assert saved.status_code == 200, saved.text
    top = (await client.get("/api/v1/home/settings")).json()["data"]["navigation"]["top"]
    # Both stay in the list, in order; only the first is off.
    assert [(e["label"], e["enabled"]) for e in top] == [("LÜLELER", False), ("KÖMÜR", True)]


async def test_hero_slide_free_layout(client: AsyncClient, admin_user: User, cleanup):
    h = auth_header(admin_user)
    slide = {"title": f"Serbest {TAG}", "cta_label": "Git", "cta_href": "/catalog"}
    created = (await client.post("/api/v1/admin/hero-slides", headers=h, json=slide)).json()
    assert created["layout"] is None  # the default centred stack

    layout = {
        "eyebrow": {"x": 12.5, "y": 20},
        "title": {"x": 30, "y": 40},
        "subtitle": {"x": 70, "y": 60},
        "button": {"x": 85.2, "y": 82},
    }
    saved = await client.put(f"/api/v1/admin/hero-slides/{created['id']}", headers=h, json={**slide, "layout": layout})
    assert saved.status_code == 200, saved.text
    assert saved.json()["layout"] == layout
    public = (await client.get("/api/v1/home/content")).json()["slides"]
    assert next(s for s in public if s["id"] == created["id"])["layout"] == layout

    # All four parts or none, and never off the slide.
    for bad in ({**layout, "button": {"x": 101, "y": 5}}, {"title": {"x": 1, "y": 1}}):
        refused = await client.put(f"/api/v1/admin/hero-slides/{created['id']}", headers=h, json={**slide, "layout": bad})
        assert refused.status_code == 422

    reset = await client.put(f"/api/v1/admin/hero-slides/{created['id']}", headers=h, json={**slide, "layout": None})
    assert reset.json()["layout"] is None


async def test_promo_box_free_layout(client: AsyncClient, admin_user: User, cleanup, db_session: AsyncSession):
    from app.models.content import PromoBox

    h = auth_header(admin_user)
    box = {"title": f"Kutu {TAG}", "href": "/catalog", "row": 1, "variant": "image"}
    created = (await client.post("/api/v1/admin/promo-boxes", headers=h, json=box)).json()
    try:
        assert created["layout"] is None
        layout = {
            "eyebrow": {"x": 20, "y": 12},
            "title": {"x": 40, "y": 30},
            "text": {"x": 60, "y": 48},
            "button": {"x": 80, "y": 88},
            "image": {"x": 75, "y": 65},
        }
        saved = await client.put(f"/api/v1/admin/promo-boxes/{created['id']}", headers=h, json={**box, "layout": layout})
        assert saved.status_code == 200, saved.text
        assert saved.json()["layout"] == layout
        # A text card has no photo to place; the four text parts are required.
        without_image = {k: v for k, v in layout.items() if k != "image"}
        ok = await client.put(f"/api/v1/admin/promo-boxes/{created['id']}", headers=h, json={**box, "layout": without_image})
        assert ok.status_code == 200 and ok.json()["layout"]["image"] is None
        missing = await client.put(
            f"/api/v1/admin/promo-boxes/{created['id']}", headers=h, json={**box, "layout": {"title": {"x": 1, "y": 1}}}
        )
        assert missing.status_code == 422
    finally:
        await client.delete(f"/api/v1/admin/promo-boxes/{created['id']}", headers=h)


async def test_promo_box_photo_veil_and_crop(client: AsyncClient, admin_user: User, cleanup):
    h = auth_header(admin_user)
    box = {"title": f"Veil {TAG}", "href": "/catalog", "row": 2, "variant": "text"}
    created = (await client.post("/api/v1/admin/promo-boxes", headers=h, json=box)).json()
    try:
        assert (created["overlay_direction"], created["image_focus_x"], created["image_focus_y"], created["image_zoom"]) == ("to-t", 50, 50, 100)
        changed = {**box, "overlay_direction": "to-r", "image_focus_x": 10, "image_focus_y": 90, "image_zoom": 140}
        saved = await client.put(f"/api/v1/admin/promo-boxes/{created['id']}", headers=h, json=changed)
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert (body["overlay_direction"], body["image_focus_x"], body["image_focus_y"], body["image_zoom"]) == ("to-r", 10, 90, 140)
        for bad in ({"overlay_direction": "diagonal"}, {"image_zoom": 50}, {"image_focus_x": 101}):
            refused = await client.put(f"/api/v1/admin/promo-boxes/{created['id']}", headers=h, json={**box, **bad})
            assert refused.status_code == 422
    finally:
        await client.delete(f"/api/v1/admin/promo-boxes/{created['id']}", headers=h)


async def test_promo_social_widget(client: AsyncClient, admin_user: User, cleanup):
    h = auth_header(admin_user)
    box = {
        "title": f"Hacim büyür {TAG}",
        "eyebrow": "Ayrıcalıklar & Avantajlar",
        "href": "/#avantajlar",
        "row": 2,
        "variant": "social",
        "widget": {
            "instagram_href": "https://www.instagram.com/sultannargile_bahcelievler",
            "youtube_href": "https://www.youtube.com/@sultannargile1982",
            "anchor_label": "Avantajları keşfet",
        },
    }
    created = await client.post("/api/v1/admin/promo-boxes", headers=h, json=box)
    assert created.status_code == 201, created.text
    body = created.json()
    try:
        assert body["variant"] == "social"
        assert body["widget"]["instagram_label"] == "Instagram" and body["widget"]["youtube_label"] == "YouTube"
        public = (await client.get("/api/v1/home/content")).json()["promos"]
        assert next(p for p in public if p["id"] == body["id"])["widget"]["anchor_label"] == "Avantajları keşfet"
        bad = {**box, "widget": {**box["widget"], "instagram_href": "javascript:alert(1)"}}
        assert (await client.put(f"/api/v1/admin/promo-boxes/{body['id']}", headers=h, json=bad)).status_code == 422
    finally:
        await client.delete(f"/api/v1/admin/promo-boxes/{body['id']}", headers=h)
