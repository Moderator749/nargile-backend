"""A category page's banner: stored per category, read by the storefront, and
uploaded wide enough to span the page on a retina screen."""

import io

from httpx import AsyncClient
from PIL import Image
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.catalog import Category
from app.models.user import User
from tests.conftest import auth_header

SLUG = "zz-banner-oduman"
BANNER = "/media/banners/oduman-3f9a1c2e.webp"


async def _cleanup(db: AsyncSession):
    await db.rollback()
    await db.execute(delete(Category).where(Category.slug == SLUG))
    await db.commit()


async def test_a_banner_is_saved_kept_and_removed(client: AsyncClient, admin_user: User, db_session: AsyncSession):
    headers = auth_header(admin_user)
    try:
        created = await client.post(
            "/api/v1/admin/categories", headers=headers, json={"name": "Oduman Nargile", "slug": SLUG, "banner_url": BANNER}
        )
        assert created.status_code == 201, created.text
        category = created.json()
        assert category["banner_url"] == BANNER

        public = await client.get(f"/api/v1/categories/by-slug/{SLUG}")
        assert public.json()["banner_url"] == BANNER
        listed = {c["slug"]: c for c in (await client.get("/api/v1/categories/")).json()}
        assert listed[SLUG]["banner_url"] == BANNER

        # A save that leaves it out (an older form) keeps it.
        kept = await client.put(f"/api/v1/admin/categories/{category['id']}", headers=headers, json={"name": "Oduman Nargile", "slug": SLUG})
        assert kept.json()["banner_url"] == BANNER

        # Not an address: refused.
        bad = await client.put(
            f"/api/v1/admin/categories/{category['id']}", headers=headers, json={"name": "Oduman Nargile", "slug": SLUG, "banner_url": "javascript:alert(1)"}
        )
        assert bad.status_code == 422

        # Null (or blank) removes it.
        removed = await client.put(
            f"/api/v1/admin/categories/{category['id']}", headers=headers, json={"name": "Oduman Nargile", "slug": SLUG, "banner_url": " "}
        )
        assert removed.json()["banner_url"] is None
        assert (await client.get(f"/api/v1/categories/by-slug/{SLUG}")).json()["banner_url"] is None
    finally:
        await _cleanup(db_session)


def _png(size: tuple[int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (40, 28, 20)).save(out, "PNG")
    return out.getvalue()


async def test_a_banner_upload_is_a_wide_webp(client: AsyncClient, admin_user: User, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_ROOT", tmp_path)
    resp = await client.post(
        "/api/v1/admin/uploads/banner", headers=auth_header(admin_user), files={"file": ("banner.png", _png((3200, 800)), "image/png")}
    )
    assert resp.status_code == 201, resp.text
    saved = resp.json()
    assert saved["path"].startswith("banners/") and saved["path"].endswith(".webp")
    assert saved["url"].endswith(saved["path"])
    with Image.open(tmp_path / saved["path"]) as image:
        # 2560px wide, not a product photo's 1800: proportions kept.
        assert image.format == "WEBP" and image.size == (2560, 640)


async def test_a_banner_upload_needs_an_admin_and_an_image(client: AsyncClient, admin_user: User, customer_user: User, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_ROOT", tmp_path)
    upload = lambda headers, data: client.post("/api/v1/admin/uploads/banner", headers=headers, files={"file": ("b.png", data, "image/png")})  # noqa: E731
    assert (await upload({}, _png((800, 200)))).status_code == 401
    assert (await upload(auth_header(customer_user), _png((800, 200)))).status_code == 403
    assert (await upload(auth_header(admin_user), b"not an image")).status_code == 422
