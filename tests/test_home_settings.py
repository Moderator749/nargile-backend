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


async def test_hero_slide_design_and_an_optional_button(client: AsyncClient, admin_user: User, cleanup):
    h = auth_header(admin_user)
    # No button at all: neither label nor link is needed, and a stray link is not kept.
    slide = {"title": f"İki satır\r\nbaşlık {TAG}  ", "subtitle": " Bir\n  iki ", "cta_label": "", "cta_href": "/catalog"}
    created = await client.post("/api/v1/admin/hero-slides", headers=h, json=slide)
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["cta_label"], body["cta_href"]) == ("", "")
    assert body["title"] == f"İki satır\nbaşlık {TAG}" and body["subtitle"] == "Bir\niki"
    # An existing slide's look by default: the wash on, its own sizes and colours.
    assert (body["enable_overlay"], body["font_sizes"], body["text_colors"]) == (True, None, None)

    design = {
        "enable_overlay": False,
        "font_sizes": {"title": 57, "subtitle": "lg"},  # px; a name of the first version reads as its px
        "text_colors": {"eyebrow": "#FFD700", "title": "#000000"},
    }
    saved = await client.put(f"/api/v1/admin/hero-slides/{body['id']}", headers=h, json={**slide, **design})
    assert saved.status_code == 200, saved.text
    got = saved.json()
    assert got["enable_overlay"] is False
    assert got["font_sizes"] == {"eyebrow": None, "title": 57, "subtitle": 18}
    assert got["text_colors"] == {"eyebrow": "#FFD700", "title": "#000000", "subtitle": None}
    public = (await client.get("/api/v1/home/content")).json()["slides"]
    assert any(s["id"] == body["id"] and s["font_sizes"]["title"] == 57 for s in public)

    for bad in (
        {"cta_label": "Git", "cta_href": ""},  # a button needs its link
        {"font_sizes": {"title": "9xl"}},
        {"font_sizes": {"title": 4}},
        {"font_sizes": {"title": 500}},
        {"text_colors": {"title": "red"}},
        {"text_colors": {"title": "#fff"}},
    ):
        resp = await client.put(f"/api/v1/admin/hero-slides/{body['id']}", headers=h, json={**slide, **bad})
        assert resp.status_code == 422, (bad, resp.text)


async def test_a_slides_mobile_design_is_its_own(client: AsyncClient, admin_user: User, cleanup):
    h = auth_header(admin_user)
    layout = {p: {"x": 50, "y": 20 + i * 20} for i, p in enumerate(("eyebrow", "title", "subtitle", "button"))}
    slide = {"title": f"Mobil {TAG}", "layout": layout, "font_sizes": {"title": 64}, "image_focus_x": 30}
    created = (await client.post("/api/v1/admin/hero-slides", headers=h, json=slide)).json()
    # Until designed, a phone shows the slide as before.
    assert (created["mobile_layout"], created["mobile_font_sizes"], created["mobile_bg_position"]) == (None, None, None)

    mobile = {
        "mobile_layout": {**layout, "title": {"x": 40, "y": 70}},
        "mobile_font_sizes": {"title": 30, "eyebrow": "sm"},
        "mobile_bg_position": {"x": 80, "y": 10, "zoom": 140},
    }
    saved = await client.put(f"/api/v1/admin/hero-slides/{created['id']}", headers=h, json={**slide, **mobile})
    assert saved.status_code == 200, saved.text
    got = saved.json()
    assert got["mobile_layout"]["title"] == {"x": 40, "y": 70} and got["mobile_font_sizes"] == {"eyebrow": 14, "title": 30, "subtitle": None}
    assert got["mobile_bg_position"] == {"x": 80, "y": 10, "zoom": 140}
    # The wide screen's design is untouched by the phone's.
    assert got["layout"]["title"] == layout["title"] and got["font_sizes"]["title"] == 64 and got["image_focus_x"] == 30

    for bad in (
        {"mobile_bg_position": {"x": 120, "y": 0}},
        {"mobile_bg_position": {"x": 10, "y": 10, "zoom": 50}},
        {"mobile_layout": {"title": {"x": 1, "y": 1}}},  # all four parts or none
        {"mobile_font_sizes": {"title": 400}},
    ):
        resp = await client.put(f"/api/v1/admin/hero-slides/{created['id']}", headers=h, json={**slide, **bad})
        assert resp.status_code == 422, (bad, resp.text)


async def test_a_slide_can_be_its_image_alone(client: AsyncClient, admin_user: User, cleanup, db_session: AsyncSession):
    h = auth_header(admin_user)
    # No eyebrow, title, subtitle or button: given as empty, as null, or left out.
    for body in ({"title": "", "eyebrow": "", "cta_label": ""}, {"title": None, "eyebrow": None}, {}):
        created = await client.post("/api/v1/admin/hero-slides", headers=h, json={**body, "image_url": "https://cdn.example.com/b.jpg"})
        assert created.status_code == 201, created.text
        got = created.json()
        assert (got["eyebrow"], got["title"], got["subtitle"], got["cta_label"], got["cta_href"]) == ("", "", "", "", "")
        assert got["image"] == "https://cdn.example.com/b.jpg"
    # Blank space alone is no title either.
    spaced = (await client.post("/api/v1/admin/hero-slides", headers=h, json={"title": "  \n  "})).json()
    assert spaced["title"] == ""
    public = (await client.get("/api/v1/home/content")).json()["slides"]
    assert sum(1 for s in public if s["title"] == "") >= 4
    await db_session.execute(delete(HeroSlide).where(HeroSlide.title == ""))
    await db_session.commit()


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
        "hidden": ["brand_ticker", "kumbara", "kumbara", "games"],
    }
    saved = await client.put("/api/v1/admin/home/settings", headers=auth_header(admin_user), json={"mobile": mobile})
    assert saved.status_code == 200, saved.text

    public = (await client.get("/api/v1/home/settings")).json()["data"]["mobile"]
    assert public["hero_images"][1] == {"slide_id": 5, "image": "/images/hero-mobil.jpg"}
    # A switch sent twice is stored once.
    assert public["hidden"] == ["brand_ticker", "kumbara", "games"]


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
    order = ["about", "games", "deals", "perks", "kozlukler", "kumbara"]
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
    assert (sections["kozlukler"], sections["games"]) == (True, True)  # new bands start on

    await client.put("/api/v1/admin/home/settings", headers=h, json={"sections": {"brands": False, "brands_top": True}})
    sections = (await client.get("/api/v1/home/settings")).json()["data"]["sections"]
    assert (sections["brands"], sections["brands_top"]) == (False, True)


SECTIONS_COPY = {
    "perks": {
        "installments": {"icon": "credit-card", "label": "Taksit", "value": "6 taksit", "note": "Tüm kartlara."},
        "volume": {"icon": "store", "label": "Sepette İndirim", "note": ""},
        "transfer": {"icon": "landmark", "label": "Havale", "note": "Nakit indirimi."},
    },
    "picks": {
        "title": "Seçtiklerimiz", "subtitle": "", "bestsellers": "Çok Satan", "newest": "Yeni",
        "shipping_eyebrow": "Ücretsiz Kargo", "shipping_text": "{tutar} üzeri ücretsiz.",
    },
    "lules": {
        "title": "Lüleler", "kinds": ["Sırlı", "Cam"], "closing": "Hepsi burada", "more": "Tümü",
        "href": "/catalog/luleler", "courier_eyebrow": "Kurye", "courier_text": "Aynı gün",
    },
    "sultanpuan": {"subtitle": "{bant} alışverişlerde", "note": "Puan indirim olur.", "call": "Kazanın!"},
    "kumbara": {"subtitle": "Her alışverişe puan", "note": "{tutar} barajı, {oran} ekstra.", "call": "Biriktirin!"},
    "campaigns": {
        "lead": {"eyebrow": "Sınırlı", "title": "Büyük Fırsat", "subtitle": "", "cta": "İncele", "href": "/firsat-urunleri",
                 "image": "/images/kampanya.jpg"},
        "minis": [{"title": f"Kutu {i}", "subtitle": "", "href": "/catalog"} for i in range(3)],
    },
    "statement": {"heading": "Referans", "lead": "Bir cümle.", "rest": "Devamı."},
    "brand_showcase": {
        "heading": "Markalarımız", "collection": "Keşfet",
        "sultan": {"description": "Sultan.", "logo": None}, "oduman": {"description": "Oduman."},
        "legend": {"description": "Legend.", "logo": "https://cdn.example.com/legend.png"},
    },
    "kozlukler": {"title": "Közlükler", "subtitle": "", "more": "Tümü"},
    "games": {"title": "Oyunlar", "subtitle": "Tavla", "more": "Tümü"},
}


async def test_every_sections_copy_is_saved_and_read_back(client: AsyncClient, admin_user: User, cleanup):
    saved = await client.put("/api/v1/admin/home/settings", headers=auth_header(admin_user), json=SECTIONS_COPY)
    assert saved.status_code == 200, saved.text
    public = (await client.get("/api/v1/home/settings")).json()["data"]
    assert set(public) == set(SECTIONS_COPY)
    assert public["perks"]["installments"]["value"] == "6 taksit"
    assert public["campaigns"]["lead"]["image"] == "/images/kampanya.jpg" and public["campaigns"]["minis"][2].get("image") is None
    assert public["brand_showcase"]["legend"]["logo"] == "https://cdn.example.com/legend.png"
    assert public["kumbara"]["note"] == "{tutar} barajı, {oran} ekstra."


@pytest.mark.parametrize(
    "bad",
    [
        {"campaigns": {**SECTIONS_COPY["campaigns"], "minis": SECTIONS_COPY["campaigns"]["minis"][:2]}},
        {"campaigns": {**SECTIONS_COPY["campaigns"], "lead": {**SECTIONS_COPY["campaigns"]["lead"], "href": "javascript:x"}}},
        {"lules": {**SECTIONS_COPY["lules"], "title": ""}},
        {"perks": {**SECTIONS_COPY["perks"], "volume": {"icon": "rocket", "label": "X", "note": ""}}},
        {"brand_showcase": {**SECTIONS_COPY["brand_showcase"], "sultan": {"description": "", "logo": "data:image/png;base64,AA"}}},
    ],
)
async def test_every_sections_copy_is_held_to_its_rules(client: AsyncClient, admin_user: User, cleanup, bad: dict):
    resp = await client.put("/api/v1/admin/home/settings", headers=auth_header(admin_user), json=bad)
    assert resp.status_code == 422, resp.text
