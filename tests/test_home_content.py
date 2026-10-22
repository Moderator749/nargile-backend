"""Homepage CMS: hero slides and the promo box (public read + admin)."""

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.content import HeroSlide, PromoBox
from app.models.user import User
from tests.conftest import auth_header

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
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
