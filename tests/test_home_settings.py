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


async def test_promo_social_widget(client: AsyncClient, admin_user: User, cleanup):
    h = auth_header(admin_user)
    box = {
        "title": f"Hacim büyür {TAG}",
        "eyebrow": "Ayrıcalıklar & Avantajlar",
        "href": "/#avantajlar",
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
        assert body["widget"]["instagram_label"] == "Instagram" and body["widget"]["youtube_label"] == "YouTube"
        public = (await client.get("/api/v1/home/content")).json()["promos"]
        assert next(p for p in public if p["id"] == body["id"])["widget"]["anchor_label"] == "Avantajları keşfet"
        bad = {**box, "widget": {**box["widget"], "instagram_href": "javascript:alert(1)"}}
        assert (await client.put(f"/api/v1/admin/promo-boxes/{body['id']}", headers=h, json=bad)).status_code == 422
    finally:
        await client.delete(f"/api/v1/admin/promo-boxes/{body['id']}", headers=h)


async def test_the_seo_passage_is_saved_and_read_back(client: AsyncClient, admin_user: User, cleanup):
    """"Alt SEO Metni": title, the paragraph shown, and what "Devamını Oku" opens."""
    seo = {
        "title": "  Nargile Malzemelerinde Tek Adres ",
        "summary": "İlk paragraf.",
        "content": "İkinci paragraf.\n\nÜçüncü paragraf.",
    }
    saved = await client.put("/api/v1/admin/home/settings", headers=auth_header(admin_user), json={"seo_intro": seo})
    assert saved.status_code == 200, saved.text
    public = (await client.get("/api/v1/home/settings")).json()["data"]["seo_intro"]
    assert public == {**seo, "title": "Nargile Malzemelerinde Tek Adres"}

    # Empty fields are allowed: the storefront fills them from its own copy.
    blank = await client.put(
        "/api/v1/admin/home/settings", headers=auth_header(admin_user), json={"seo_intro": {"title": "", "summary": "", "content": ""}}
    )
    assert blank.status_code == 200, blank.text


@pytest.mark.parametrize(
    "seo",
    [
        {"title": "x" * 151, "summary": "", "content": ""},
        {"title": "", "summary": "x" * 1501, "content": ""},
        {"title": "", "summary": "", "content": "x" * 6001},
        {"title": "", "summary": "", "content": "", "extra": "?"},
    ],
)
async def test_the_seo_passage_is_held_to_its_limits(client: AsyncClient, admin_user: User, cleanup, seo: dict):
    resp = await client.put("/api/v1/admin/home/settings", headers=auth_header(admin_user), json={"seo_intro": seo})
    assert resp.status_code == 422



async def test_the_mobile_settings_are_saved_and_read_back(client: AsyncClient, admin_user: User, cleanup):
    mobile = {
        "hero_images": [
            {"slide_id": 3, "image": "https://cdn.example.com/hero-dikey.jpg"},
            {"slide_id": 5, "image": " /images/hero-mobil.jpg "},
        ],
        "hidden": ["brand_ticker", "kumbara", "kumbara"],
    }
    saved = await client.put("/api/v1/admin/home/settings", headers=auth_header(admin_user), json={"mobile": mobile})
    assert saved.status_code == 200, saved.text

    public = (await client.get("/api/v1/home/settings")).json()["data"]["mobile"]
    assert public["hero_images"][1] == {"slide_id": 5, "image": "/images/hero-mobil.jpg"}
    # A switch sent twice is stored once.
    assert public["hidden"] == ["brand_ticker", "kumbara"]


@pytest.mark.parametrize(
    "mobile",
    [
        {"hidden": ["footer"]},  # not a section a phone can go without
        {"hero_images": [{"slide_id": 1, "image": ""}]},
        {"hero_images": [{"slide_id": 1, "image": "javascript:alert(1)"}]},
        {"hero_images": [{"slide_id": 1, "image": "/a.jpg"}, {"slide_id": 1, "image": "/b.jpg"}]},
        {"hero_images": [{"slide_id": 0, "image": "/a.jpg"}]},
    ],
)
async def test_the_mobile_settings_are_held_to_their_rules(
    client: AsyncClient, admin_user: User, cleanup, mobile: dict
):
    response = await client.put("/api/v1/admin/home/settings", headers=auth_header(admin_user), json={"mobile": mobile})
    assert response.status_code == 422


async def test_the_sections_order_is_saved_and_read_back(client: AsyncClient, admin_user: User, cleanup):
    order = ["about", "deals", "perks", "kumbara"]
    saved = await client.put("/api/v1/admin/home/settings", headers=auth_header(admin_user), json={"order": order})
    assert saved.status_code == 200, saved.text
    assert (await client.get("/api/v1/home/settings")).json()["data"]["order"] == order


@pytest.mark.parametrize(
    "order",
    [
        ["deals", "deals"],  # twice
        ["hero", "deals"],  # always first, not in the order
        ["footer"],  # always last
        ["nope"],
    ],
)
async def test_the_sections_order_is_held_to_its_rules(client: AsyncClient, admin_user: User, cleanup, order: list):
    response = await client.put("/api/v1/admin/home/settings", headers=auth_header(admin_user), json={"order": order})
    assert response.status_code == 422


async def test_the_top_brand_ticker_follows_an_older_brands_switch(client: AsyncClient, admin_user: User, cleanup):
    h = auth_header(admin_user)
    await client.put("/api/v1/admin/home/settings", headers=h, json={"sections": {"brands": False}})
    sections = (await client.get("/api/v1/home/settings")).json()["data"]["sections"]
    assert (sections["brands"], sections["brands_top"], sections["kumbara"]) == (False, False, True)

    await client.put("/api/v1/admin/home/settings", headers=h, json={"sections": {"brands": False, "brands_top": True}})
    sections = (await client.get("/api/v1/home/settings")).json()["data"]["sections"]
    assert (sections["brands"], sections["brands_top"]) == (False, True)
