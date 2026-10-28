"""Homepage CMS: hero slides and the promo box (public read + admin)."""

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.content import HeroSlide, PromoBox
from app.models.user import User
from tests.conftest import auth_header

def _real_png() -> bytes:
    """A real, tiny PNG: an upload is decoded and stored as a WebP now, so magic bytes alone no longer do."""
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(out, "PNG")
    return out.getvalue()


PNG = _real_png()
TAG = "⟪test⟫"  # marks rows created here, so cleanup never touches real content


@pytest.fixture
async def cleanup(db_session: AsyncSession):
    yield
    await db_session.rollback()
    await db_session.execute(delete(HeroSlide).where(HeroSlide.title.contains(TAG)))
    await db_session.execute(delete(PromoBox).where(PromoBox.title.contains(TAG)))
    await db_session.commit()


SLIDE = {"title": f"Başlık {TAG}", "cta_label": "Keşfet", "cta_href": "/catalog", "sort_order": 1}
PROMO = {
    "title": f"Kutu {TAG}",
    "href": "/#avantajlar",
    "widget": {"instagram_href": "https://www.instagram.com/x", "anchor_label": "Keşfet"},
}


async def test_public_content_lists_active_items_in_order(
    client: AsyncClient, admin_user: User, cleanup
):
    h = auth_header(admin_user)
    shown = (await client.post("/api/v1/admin/hero-slides", headers=h, json=SLIDE)).json()
    hidden = (
        await client.post("/api/v1/admin/hero-slides", headers=h, json={**SLIDE, "is_active": False})
    ).json()

    content = (await client.get("/api/v1/home/content")).json()
    ids = [s["id"] for s in content["slides"]]
    assert shown["id"] in ids and hidden["id"] not in ids
    orders = [s["sort_order"] for s in content["slides"]]
    assert orders == sorted(orders)
    assert set(content) == {"slides", "promos"}


@pytest.mark.parametrize(
    ("path", "body", "patch"),
    [
        ("/api/v1/admin/hero-slides", SLIDE, {"subtitle": "Yeni alt metin"}),
        ("/api/v1/admin/promo-boxes", PROMO, {"eyebrow": "Ayrıcalıklar"}),
    ],
)
async def test_admin_crud(client: AsyncClient, admin_user: User, cleanup, path: str, body: dict, patch: dict):
    h = auth_header(admin_user)
    created = await client.post(path, headers=h, json=body)
    assert created.status_code == 201, created.text
    row = created.json()

    updated = await client.put(f"{path}/{row['id']}", headers=h, json={**body, **patch})
    assert updated.status_code == 200, updated.text
    key, value = next(iter(patch.items()))
    assert updated.json()[key] == value

    assert any(r["id"] == row["id"] for r in (await client.get(path, headers=h)).json())
    assert (await client.delete(f"{path}/{row['id']}", headers=h)).status_code == 204
    assert (await client.put(f"{path}/{row['id']}", headers=h, json=body)).status_code == 404


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/admin/hero-slides", {**SLIDE, "cta_href": "javascript:alert(1)"}),
        ("/api/v1/admin/hero-slides", {**SLIDE, "cta_href": "//evil.example"}),
        ("/api/v1/admin/hero-slides", {**SLIDE, "cta_href": "http://insecure.example"}),
        ("/api/v1/admin/hero-slides", {**SLIDE, "guest_cta_label": "Bayi"}),  # label without link
        ("/api/v1/admin/hero-slides", {**SLIDE, "image_url": "https://x.com/a.png\")"}),
        ("/api/v1/admin/hero-slides", {**SLIDE, "requires": "moon"}),
        ("/api/v1/admin/hero-slides", {**SLIDE, "title": "x" * 121}),
        ("/api/v1/admin/promo-boxes", {**PROMO, "href": "data:text/html,x"}),
        ("/api/v1/admin/promo-boxes", {k: v for k, v in PROMO.items() if k != "widget"}),  # the box is its widget
        ("/api/v1/admin/promo-boxes", {**PROMO, "widget": {"youtube_href": "javascript:alert(1)"}}),
    ],
)
async def test_admin_validation(client: AsyncClient, admin_user: User, cleanup, path: str, body: dict):
    assert (await client.post(path, headers=auth_header(admin_user), json=body)).status_code == 422


async def test_image_upload_wins_over_url(client: AsyncClient, admin_user: User, cleanup, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_ROOT", tmp_path)
    h = auth_header(admin_user)
    path = "/api/v1/admin/hero-slides"
    body = {**SLIDE, "image_url": "https://cdn.example.com/a.jpg"}
    row = (await client.post(path, headers=h, json=body)).json()
    assert row["image"] == "https://cdn.example.com/a.jpg" and row["has_upload"] is False

    up = await client.post(f"{path}/{row['id']}/image", headers=h, files={"file": ("a.png", PNG, "image/png")})
    assert up.status_code == 200, up.text
    assert "/media/" in up.json()["image"] and up.json()["has_upload"] is True

    bad = await client.post(f"{path}/{row['id']}/image", headers=h, files={"file": ("a.svg", b"<svg/>", "image/svg+xml")})
    assert bad.status_code == 422

    removed = await client.delete(f"{path}/{row['id']}/image", headers=h)
    assert removed.json()["image"] == "https://cdn.example.com/a.jpg"  # falls back to the URL
    assert not any((tmp_path / path.rsplit("/", 1)[-1]).iterdir())


@pytest.mark.parametrize("path", ["/api/v1/admin/hero-slides", "/api/v1/admin/promo-boxes"])
async def test_content_admin_requires_admin(client: AsyncClient, customer_user: User, path: str):
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers=auth_header(customer_user))).status_code == 403


async def test_the_retired_content_is_gone(client: AsyncClient, admin_user: User, cleanup):
    """The CMS advantages ("Avantajlar" lives in Ana Sayfa Düzenleme) and the promo box's photo."""
    h = auth_header(admin_user)
    assert (await client.get("/api/v1/admin/advantages", headers=h)).status_code == 404
    row = (await client.post("/api/v1/admin/promo-boxes", headers=h, json=PROMO)).json()
    up = await client.post(f"/api/v1/admin/promo-boxes/{row['id']}/image", headers=h, files={"file": ("a.png", PNG, "image/png")})
    assert up.status_code in (404, 405)


async def test_each_promo_card_takes_its_own_photo(client: AsyncClient, admin_user: User, cleanup):
    """A photo per card (a site path or an https URL) and its crop; none = the shop's own photo."""
    h = auth_header(admin_user)
    row = (await client.post("/api/v1/admin/promo-boxes", headers=h, json=PROMO)).json()
    assert (row["top_card_image_url"], row["top_card_focus_x"], row["bottom_card_image_url"], row["bottom_card_focus_x"]) == (None, 50, None, 50)

    photos = {
        "top_card_image_url": "https://cdn.example.com/kanallar.jpg", "top_card_focus_x": 20,
        "bottom_card_image_url": "/api/backend/media/uploads/avantaj.webp", "bottom_card_focus_x": 75,
    }
    saved = await client.put(f"/api/v1/admin/promo-boxes/{row['id']}", headers=h, json={**PROMO, **photos})
    assert saved.status_code == 200, saved.text
    public = next(p for p in (await client.get("/api/v1/home/content")).json()["promos"] if p["id"] == row["id"])
    assert {k: public[k] for k in photos} == photos

    # Blanked, a card goes back to the shop's own photo.
    cleared = await client.put(f"/api/v1/admin/promo-boxes/{row['id']}", headers=h, json={**PROMO, **photos, "top_card_image_url": "  "})
    assert cleared.json()["top_card_image_url"] is None and cleared.json()["bottom_card_image_url"] == photos["bottom_card_image_url"]


@pytest.mark.parametrize(
    "bad",
    [
        {"top_card_image_url": "javascript:alert(1)"},
        {"bottom_card_image_url": "//evil.example/x.jpg"},
        {"top_card_focus_x": 101},
        {"bottom_card_focus_x": -1},
    ],
)
async def test_promo_card_photos_are_held_to_their_rules(client: AsyncClient, admin_user: User, cleanup, bad: dict):
    resp = await client.post("/api/v1/admin/promo-boxes", headers=auth_header(admin_user), json={**PROMO, **bad})
    assert resp.status_code == 422, resp.text


async def test_each_slide_sizes_its_own_buttons(client: AsyncClient, admin_user: User, cleanup):
    """Desktop and phone each: sm, md or lg; a slide saved without them has the shop's own (md)."""
    h = auth_header(admin_user)
    row = (await client.post("/api/v1/admin/hero-slides", headers=h, json=SLIDE)).json()
    assert (row["button_size_desktop"], row["button_size_mobile"]) == ("md", "md")
    saved = await client.put(f"/api/v1/admin/hero-slides/{row['id']}", headers=h, json={**SLIDE, "button_size_desktop": "lg", "button_size_mobile": "sm"})
    assert saved.status_code == 200, saved.text
    public = next(s for s in (await client.get("/api/v1/home/content")).json()["slides"] if s["id"] == row["id"])
    assert (public["button_size_desktop"], public["button_size_mobile"]) == ("lg", "sm")
    bad = await client.put(f"/api/v1/admin/hero-slides/{row['id']}", headers=h, json={**SLIDE, "button_size_mobile": "xl"})
    assert bad.status_code == 422



async def test_the_promo_box_is_one_box_saved_where_the_homepage_shows_it(
    client: AsyncClient, admin_user: User, cleanup
):
    """"Promo Kutusu" edits the box the homepage shows; once saved it is the only
    one shown, every text and each card's shade included."""
    h = auth_header(admin_user)
    # Two boxes on, as the panel's old list left the shop with.
    first = (await client.post("/api/v1/admin/promo-boxes", headers=h, json={**PROMO, "sort_order": 0})).json()
    other = (await client.post("/api/v1/admin/promo-boxes", headers=h, json={**PROMO, "sort_order": 1})).json()
    current = (await client.get("/api/v1/admin/promo-box", headers=h)).json()
    assert current["is_active"] is True

    body = {
        **PROMO,
        "title": f"Kazanan Siz Olun {TAG}",
        "text": "Bizi takip edin",
        "top_title": "Topluluğumuza katılın",
        "bottom_text": "Sultan Nargile ile alışverişlerinizde toptan ve perakende ayrıcalıklar.",
        "top_shade": "bottom",
        "bottom_shade": "right",
        "requires": "flash_sales",  # a single box has no condition: ignored
        "is_active": False,  # nor can it be switched off from here
    }
    saved = await client.put("/api/v1/admin/promo-box", headers=h, json=body)
    assert saved.status_code == 200, saved.text
    box = saved.json()
    assert box["id"] == current["id"]
    assert (box["top_title"], box["bottom_text"], box["top_shade"], box["bottom_shade"]) == (
        "Topluluğumuza katılın", body["bottom_text"], "bottom", "right",
    )
    assert (box["is_active"], box["requires"]) == (True, None)

    # The homepage shows exactly what was saved, and nothing else.
    promos = (await client.get("/api/v1/home/content")).json()["promos"]
    assert [p["id"] for p in promos] == [box["id"]]
    assert promos[0]["top_title"] == "Topluluğumuza katılın" and promos[0]["bottom_shade"] == "right"
    # The other box is switched off, not deleted.
    rows = {r["id"]: r for r in (await client.get("/api/v1/admin/promo-boxes", headers=h)).json()}
    for row_id in {first["id"], other["id"]} - {box["id"]}:
        assert rows[row_id]["is_active"] is False

    # Blank texts go back to the storefront's own copy.
    cleared = (await client.put("/api/v1/admin/promo-box", headers=h, json={**body, "top_title": " ", "bottom_text": ""})).json()
    assert (cleared["top_title"], cleared["bottom_text"]) == (None, None)
    assert (await client.get("/api/v1/admin/promo-box", headers=h)).json()["id"] == box["id"]


@pytest.mark.parametrize("bad", [{"top_shade": "diagonal"}, {"bottom_shade": ""}, {"bottom_text": "x" * 301}])
async def test_promo_box_shades_and_texts_are_held_to_their_rules(client: AsyncClient, admin_user: User, cleanup, bad: dict):
    resp = await client.put("/api/v1/admin/promo-box", headers=auth_header(admin_user), json={**PROMO, **bad})
    assert resp.status_code == 422


async def test_the_promo_box_is_for_admins(client: AsyncClient, customer_user: User):
    assert (await client.get("/api/v1/admin/promo-box", headers=auth_header(customer_user))).status_code == 403
    assert (await client.put("/api/v1/admin/promo-box", headers=auth_header(customer_user), json=PROMO)).status_code == 403


async def test_slides_are_put_in_order_by_dragging(client: AsyncClient, admin_user: User, cleanup):
    """The panel sends every slide, first to last; the homepage shows them in
    that order, so the first one is its <h1>. An edit afterwards keeps it."""
    h = auth_header(admin_user)
    made = [
        (await client.post("/api/v1/admin/hero-slides", headers=h, json={**SLIDE, "title": f"{n} {TAG}", "sort_order": 10 * i})).json()
        for i, n in enumerate(["Bir", "İki", "Üç"], start=1)
    ]
    every = [r["id"] for r in (await client.get("/api/v1/admin/hero-slides", headers=h)).json()]
    mine = [r["id"] for r in made]
    # The last of mine to the very front, the others after it as they were.
    wanted = [mine[2], *[i for i in every if i != mine[2]]]
    resp = await client.put("/api/v1/admin/hero-slides/order", headers=h, json={"ids": wanted})
    assert resp.status_code == 200, resp.text
    assert [r["id"] for r in resp.json()] == wanted
    assert [r["sort_order"] for r in resp.json()] == [10 * (i + 1) for i in range(len(wanted))]

    shown = [s["id"] for s in (await client.get("/api/v1/home/content")).json()["slides"]]
    assert shown[0] == mine[2]
    # Editing a slide with its new place keeps the order.
    moved = next(r for r in resp.json() if r["id"] == mine[2])
    edit = await client.put(f"/api/v1/admin/hero-slides/{mine[2]}", headers=h, json={**SLIDE, "title": f"Üç (yeni) {TAG}", "sort_order": moved["sort_order"]})
    assert edit.status_code == 200
    assert [s["id"] for s in (await client.get("/api/v1/home/content")).json()["slides"]][0] == mine[2]


@pytest.mark.parametrize("trim", ["missing", "twice", "unknown"])
async def test_a_slide_order_must_list_every_slide_once(client: AsyncClient, admin_user: User, cleanup, trim: str):
    h = auth_header(admin_user)
    await client.post("/api/v1/admin/hero-slides", headers=h, json=SLIDE)
    every = [r["id"] for r in (await client.get("/api/v1/admin/hero-slides", headers=h)).json()]
    before = [(r["id"], r["sort_order"]) for r in (await client.get("/api/v1/admin/hero-slides", headers=h)).json()]
    ids = {"missing": every[1:] or [], "twice": [*every, every[0]], "unknown": [*every, 999_999]}[trim]
    resp = await client.put("/api/v1/admin/hero-slides/order", headers=h, json={"ids": ids or [999_999]})
    assert resp.status_code == 409
    after = [(r["id"], r["sort_order"]) for r in (await client.get("/api/v1/admin/hero-slides", headers=h)).json()]
    assert after == before


async def test_reordering_slides_is_for_admins(client: AsyncClient, customer_user: User):
    resp = await client.put("/api/v1/admin/hero-slides/order", headers=auth_header(customer_user), json={"ids": [1]})
    assert resp.status_code == 403


async def test_a_wide_slide_keeps_2560px_and_is_stored_as_webp(client: AsyncClient, admin_user: User, cleanup, tmp_path, monkeypatch):
    """A hero slide is drawn edge to edge on a wide screen: it keeps up to 2560px (a product photo, 1800)."""
    import io

    from PIL import Image

    monkeypatch.setattr(settings, "MEDIA_ROOT", tmp_path)
    h = auth_header(admin_user)
    row = (await client.post("/api/v1/admin/hero-slides", headers=h, json=SLIDE)).json()
    photo = io.BytesIO()
    Image.new("RGB", (3200, 1600), (30, 30, 30)).save(photo, "JPEG", quality=95)
    up = await client.post(f"/api/v1/admin/hero-slides/{row['id']}/image", headers=h, files={"file": ("hero.jpg", photo.getvalue(), "image/jpeg")})
    assert up.status_code == 200, up.text
    [stored] = list((tmp_path / "hero-slides").iterdir())
    assert stored.suffix == ".webp"
    with Image.open(stored) as image:
        assert image.size == (2560, 1280)
