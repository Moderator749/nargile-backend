"""Category tree (menu), product → category mapping, brands and logo uploads."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.brand import Brand
from app.models.catalog import Category, Product, ProductCategory
from app.models.user import User
from app.schemas.taxonomy import slugify
from tests.conftest import auth_header

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def tag() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
async def cleanup(db_session: AsyncSession, tag: str):
    """Remove categories/brands whose slug carries this test's tag."""
    yield
    await db_session.rollback()
    await db_session.execute(
        Product.__table__.update()
        .where(Product.category_id.in_(select(Category.id).where(Category.slug.like(f"%{tag}%"))))
        .values(category_id=None)
    )
    await db_session.execute(
        delete(ProductCategory).where(
            ProductCategory.category_id.in_(select(Category.id).where(Category.slug.like(f"%{tag}%")))
        )
    )
    await db_session.execute(
        Category.__table__.update().where(Category.slug.like(f"%{tag}%")).values(parent_id=None)
    )
    await db_session.execute(delete(Category).where(Category.slug.like(f"%{tag}%")))
    await db_session.execute(delete(Brand).where(Brand.slug.like(f"%{tag}%")))
    await db_session.commit()


async def _create_category(client, admin, **body):
    return await client.post("/api/v1/admin/categories", headers=auth_header(admin), json=body)


def test_slugify_turkish():
    assert slugify("Çelik Takımlar") == "celik-takimlar"
    assert slugify("Tüketim & Sarf") == "tuketim-sarf"
    assert slugify("  Yedek Şişeler!! ") == "yedek-siseler"


# ── Category tree ────────────────────────────────────────────────────────────


async def test_parent_category_lists_subcategory_products(
    client: AsyncClient, admin_user: User, catalog, tag: str, cleanup
):
    normal_v, _ = catalog
    parent = (await _create_category(client, admin_user, name=f"Grup {tag}")).json()
    child = (await _create_category(client, admin_user, name=f"Alt {tag}", parent_id=parent["id"])).json()
    assert parent["slug"] == f"grup-{tag}"

    product_id = str((await client.get("/api/v1/products/", params={"q": normal_v.sku})).json()[0]["id"])
    moved = await client.post(
        "/api/v1/admin/products/category",
        headers=auth_header(admin_user),
        json={"product_ids": [product_id], "category_id": child["id"]},
    )
    assert moved.json() == {"updated": 1}

    for slug in (child["slug"], parent["slug"]):  # the parent includes its subcategories
        resp = await client.get("/api/v1/products/", params={"category": slug})
        assert [p["id"] for p in resp.json()] == [product_id]


async def test_menu_is_two_levels(client: AsyncClient, admin_user: User, tag: str, cleanup):
    top = (await _create_category(client, admin_user, name=f"Top {tag}")).json()
    sub = (await _create_category(client, admin_user, name=f"Sub {tag}", parent_id=top["id"])).json()

    deeper = await _create_category(client, admin_user, name=f"Deep {tag}", parent_id=sub["id"])
    assert deeper.status_code == 422

    # A parent with subcategories can't be moved under another category.
    other = (await _create_category(client, admin_user, name=f"Other {tag}")).json()
    resp = await client.put(
        f"/api/v1/admin/categories/{top['id']}",
        headers=auth_header(admin_user),
        json={"name": top["name"], "slug": top["slug"], "parent_id": other["id"]},
    )
    assert resp.status_code == 422

    self_parent = await client.put(
        f"/api/v1/admin/categories/{other['id']}",
        headers=auth_header(admin_user),
        json={"name": other["name"], "parent_id": other["id"]},
    )
    assert self_parent.status_code == 422


async def test_category_slug_conflicts_and_validation(client: AsyncClient, admin_user: User, tag: str, cleanup):
    assert (await _create_category(client, admin_user, name="A", slug=f"dup-{tag}")).status_code == 201
    assert (await _create_category(client, admin_user, name="B", slug=f"dup-{tag}")).status_code == 409
    assert (await _create_category(client, admin_user, name="C", slug="Not A Slug")).status_code == 422
    assert (await _create_category(client, admin_user, name="   ")).status_code == 422


async def test_delete_category_guards(client: AsyncClient, admin_user: User, catalog, tag: str, cleanup):
    normal_v, _ = catalog
    h = auth_header(admin_user)
    top = (await _create_category(client, admin_user, name=f"Top {tag}")).json()
    sub = (await _create_category(client, admin_user, name=f"Sub {tag}", parent_id=top["id"])).json()

    assert (await client.delete(f"/api/v1/admin/categories/{top['id']}", headers=h)).status_code == 409

    product_id = (await client.get("/api/v1/products/", params={"q": normal_v.sku})).json()[0]["id"]
    await client.post("/api/v1/admin/products/category", headers=h, json={"product_ids": [product_id], "category_id": sub["id"]})
    assert (await client.delete(f"/api/v1/admin/categories/{sub['id']}", headers=h)).status_code == 409

    await client.post("/api/v1/admin/products/category", headers=h, json={"product_ids": [product_id], "category_id": None})
    assert (await client.delete(f"/api/v1/admin/categories/{sub['id']}", headers=h)).status_code == 204
    assert (await client.delete(f"/api/v1/admin/categories/{top['id']}", headers=h)).status_code == 204


async def test_admin_category_list_counts_products(client: AsyncClient, admin_user: User, catalog, tag: str, cleanup):
    normal_v, heavy_v = catalog
    h = auth_header(admin_user)
    cat = (await _create_category(client, admin_user, name=f"Count {tag}")).json()
    ids = [
        (await client.get("/api/v1/products/", params={"q": v.sku})).json()[0]["id"] for v in (normal_v, heavy_v)
    ]
    await client.post("/api/v1/admin/products/category", headers=h, json={"product_ids": ids, "category_id": cat["id"]})
    rows = (await client.get("/api/v1/admin/categories", headers=h)).json()
    assert next(r for r in rows if r["id"] == cat["id"])["product_count"] == 2


async def test_assign_to_unknown_category_is_404(client: AsyncClient, admin_user: User):
    resp = await client.post(
        "/api/v1/admin/products/category",
        headers=auth_header(admin_user),
        json={"product_ids": [str(uuid.uuid4())], "category_id": 999_999_999},
    )
    assert resp.status_code == 404


# ── Brands ───────────────────────────────────────────────────────────────────


async def test_brand_crud_and_public_list(client: AsyncClient, admin_user: User, tag: str, cleanup):
    h = auth_header(admin_user)
    created = await client.post("/api/v1/admin/brands", headers=h, json={"name": f"Mr. Wood {tag}"})
    assert created.status_code == 201, created.text
    brand = created.json()
    assert brand["slug"] == f"mr-wood-{tag}"
    assert brand["search_term"] == f"Mr. Wood {tag}"  # defaults to the name

    public = (await client.get("/api/v1/brands/")).json()
    assert any(b["id"] == brand["id"] for b in public)

    hidden = await client.put(
        f"/api/v1/admin/brands/{brand['id']}", headers=h, json={"name": brand["name"], "is_active": False}
    )
    assert hidden.json()["is_active"] is False
    assert all(b["id"] != brand["id"] for b in (await client.get("/api/v1/brands/")).json())
    assert any(b["id"] == brand["id"] for b in (await client.get("/api/v1/admin/brands", headers=h)).json())

    assert (await client.delete(f"/api/v1/admin/brands/{brand['id']}", headers=h)).status_code == 204


async def test_brand_logo_upload(client: AsyncClient, admin_user: User, tag: str, cleanup, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_ROOT", tmp_path)
    h = auth_header(admin_user)
    brand = (await client.post("/api/v1/admin/brands", headers=h, json={"name": f"Logo {tag}"})).json()
    url = f"/api/v1/admin/brands/{brand['id']}/logo"

    ok = await client.post(url, headers=h, files={"file": ("logo.svg", PNG, "image/svg+xml")})
    assert ok.status_code == 200, ok.text  # judged by content, not the claimed type/name
    first = ok.json()["logo_url"]
    assert first.endswith(".png") and "/media/brands/" in first
    stored = list((tmp_path / "brands").iterdir())
    assert len(stored) == 1

    replaced = await client.post(url, headers=h, files={"file": ("l.png", PNG, "image/png")})
    assert replaced.json()["logo_url"] != first
    assert len(list((tmp_path / "brands").iterdir())) == 1  # old file removed

    removed = await client.delete(url, headers=h)
    assert removed.json()["logo_url"] is None
    assert list((tmp_path / "brands").iterdir()) == []


async def test_uploads_are_addressed_through_public_media_url(
    client: AsyncClient, admin_user: User, tag: str, cleanup, tmp_path, monkeypatch
):
    """Behind the storefront's proxy, not at the address the request came in on."""
    monkeypatch.setattr(settings, "MEDIA_ROOT", tmp_path)
    monkeypatch.setattr(settings, "PUBLIC_MEDIA_URL", "https://shop.example/api/backend/media/")
    h = {**auth_header(admin_user), "host": "209.38.195.227"}
    brand = (await client.post("/api/v1/admin/brands", headers=h, json={"name": f"Logo {tag}"})).json()
    logo = await client.post(f"/api/v1/admin/brands/{brand['id']}/logo", headers=h, files={"file": ("l.png", PNG, "image/png")})
    assert logo.json()["logo_url"].startswith("https://shop.example/api/backend/media/brands/")

    [upload] = (await client.post("/api/v1/admin/uploads", headers=h, files={"files": ("p.png", PNG, "image/png")})).json()
    assert upload["url"] == f"https://shop.example/api/backend/media/{upload['path']}"


@pytest.mark.parametrize(
    "content",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b"GIF89a" + b"\x00" * 10,
        b"",
        b"\x89PNG\r\n\x1a\n" + b"\x00" * (1024 * 1024),  # over 1 MB
    ],
)
async def test_brand_logo_rejects_unsafe_or_large(
    client: AsyncClient, admin_user: User, tag: str, cleanup, tmp_path, monkeypatch, content: bytes
):
    monkeypatch.setattr(settings, "MEDIA_ROOT", tmp_path)
    h = auth_header(admin_user)
    brand = (await client.post("/api/v1/admin/brands", headers=h, json={"name": f"Bad {tag}"})).json()
    resp = await client.post(
        f"/api/v1/admin/brands/{brand['id']}/logo", headers=h, files={"file": ("x.png", content, "image/png")}
    )
    assert resp.status_code == 422
    assert not (tmp_path / "brands").exists() or list((tmp_path / "brands").iterdir()) == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/admin/categories"),
        ("POST", "/api/v1/admin/categories"),
        ("POST", "/api/v1/admin/products/category"),
        ("GET", "/api/v1/admin/brands"),
        ("POST", "/api/v1/admin/brands"),
        ("POST", "/api/v1/admin/brands/1/logo"),
    ],
)
async def test_taxonomy_admin_requires_admin(client: AsyncClient, customer_user: User, method: str, path: str):
    resp = await client.request(method, path, headers=auth_header(customer_user), json={})
    assert resp.status_code == 403


async def test_renamed_category_slug_redirects(client: AsyncClient, admin_user: User, tag: str, cleanup):
    """Changing a category's address in the admin keeps the old one answering."""
    created = await _create_category(client, admin_user, name=f"Eski {tag}", slug=f"eski-{tag}")
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    resp = await client.put(
        f"/api/v1/admin/categories/{cid}", headers=auth_header(admin_user),
        json={"name": f"Eski {tag}", "slug": f"yeni-{tag}", "parent_id": None, "sort_order": 0, "is_active": True, "image_url": None},
    )
    assert resp.status_code == 200, resp.text

    old = await client.get(f"/api/v1/categories/by-slug/eski-{tag}")
    assert old.status_code == 200 and old.json()["slug"] == f"yeni-{tag}"
    assert (await client.get(f"/api/v1/categories/by-slug/yeni-{tag}")).json()["id"] == cid
    assert (await client.get(f"/api/v1/categories/by-slug/hic-olmayan-{tag}")).status_code == 404


async def test_category_description(client: AsyncClient, admin_user: User, tag: str, cleanup):
    created = await _create_category(
        client, admin_user, name=f"Açıklamalı {tag}", slug=f"aciklamali-{tag}", description="  Uzun bir tanıtım.  "
    )
    assert created.status_code == 201, created.text
    assert created.json()["description"] == "<p>Uzun bir tanıtım.</p>"
    public = await client.get(f"/api/v1/categories/by-slug/aciklamali-{tag}")
    assert public.json()["description"] == "<p>Uzun bir tanıtım.</p>"
    # A save that does not mention it leaves it alone.
    cid = created.json()["id"]
    resp = await client.put(
        f"/api/v1/admin/categories/{cid}", headers=auth_header(admin_user),
        json={"name": f"Açıklamalı {tag}", "slug": f"aciklamali-{tag}", "parent_id": None, "sort_order": 0, "is_active": True, "image_url": None},
    )
    assert resp.json()["description"] == "<p>Uzun bir tanıtım.</p>"
