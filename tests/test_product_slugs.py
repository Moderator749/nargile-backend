"""Product slugs: given on insert, unique, stable, and readable by the detail endpoint."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.slugs import PRODUCT_SLUG, product_slug_base
from app.models.catalog import Product


@pytest.fixture
async def twins(db_session: AsyncSession):
    """Two products with one name, written in the same flush."""
    suffix = uuid.uuid4().hex[:6]
    first = Product(base_code=f"SLG-A-{suffix}", name=f"Çift Hortumlu Şişe İğne {suffix}", tags=[])
    second = Product(base_code=f"SLG-B-{suffix}", name=f"Çift Hortumlu Şişe İğne {suffix}", tags=[])
    db_session.add_all([first, second])
    await db_session.commit()
    yield first, second, suffix
    await db_session.execute(delete(Product).where(Product.id.in_([first.id, second.id])))
    await db_session.commit()


def test_slug_base_folds_turkish_letters():
    assert product_slug_base("Çift Hortumlu Şişe — İğne & Ölçü") == "cift-hortumlu-sise-igne-olcu"
    assert product_slug_base("!!!") == "urun"
    assert PRODUCT_SLUG.match("mr.wood-nargile-takimi")  # the old store's dotted spelling
    assert not PRODUCT_SLUG.match("bad--slug")


async def test_new_products_get_unique_slugs_from_their_name(twins):
    first, second, suffix = twins
    assert first.slug == f"cift-hortumlu-sise-igne-{suffix}"
    assert second.slug == f"{first.slug}-2"


async def test_rename_keeps_the_slug(db_session: AsyncSession, twins):
    first, _second, suffix = twins
    before = first.slug
    first.name = f"Yeni İsim {suffix}"
    await db_session.commit()
    assert first.slug == before


async def test_detail_reads_by_slug_and_by_id(client: AsyncClient, twins):
    first, *_ = twins
    by_slug = await client.get(f"/api/v1/products/{first.slug}")
    assert by_slug.status_code == 200, by_slug.text
    assert by_slug.json()["id"] == str(first.id)
    # Case does not matter in an address someone typed.
    assert (await client.get(f"/api/v1/products/{first.slug.upper()}")).status_code == 200
    # An id answers with the slug, for the page's permanent redirect.
    by_id = await client.get(f"/api/v1/products/{first.id}")
    assert by_id.status_code == 200
    assert by_id.json()["slug"] == first.slug
    assert (await client.get("/api/v1/products/no-such-product-anywhere")).status_code == 404


async def test_unpublished_product_is_not_found_by_slug(client: AsyncClient, db_session: AsyncSession, twins):
    first, *_ = twins
    first.is_published = False
    await db_session.commit()
    assert (await client.get(f"/api/v1/products/{first.slug}")).status_code == 404


async def test_renamed_product_slug_keeps_answering(client: AsyncClient, db_session: AsyncSession, twins):
    """A slug changed through the product sheet leaves the old one redirecting."""
    from app.models.catalog import SlugRedirect
    from app.services import slug_redirects

    first, *_ = twins
    old = first.slug
    await slug_redirects.moved(db_session, "product", old, f"{old}-yeni", product_id=first.id)
    first.slug = f"{old}-yeni"
    await db_session.commit()

    resp = await client.get(f"/api/v1/products/{old}")
    assert resp.status_code == 200
    assert resp.json()["slug"] == f"{old}-yeni"  # the page redirects on this

    # Renaming back: the address is live again, so it is nobody's redirect.
    await slug_redirects.moved(db_session, "product", f"{old}-yeni", old, product_id=first.id)
    first.slug = old
    await db_session.commit()
    rows = (await db_session.execute(select(SlugRedirect.old_slug).where(SlugRedirect.product_id == first.id))).scalars().all()
    assert rows == [f"{old}-yeni"]


async def test_product_sheet_edits_the_slug(client: AsyncClient, db_session: AsyncSession, admin_user):
    from tests.conftest import auth_header
    from tests.test_admin_product_grid import _save, _sheet

    suffix = uuid.uuid4().hex[:6]
    sheet = await _save(client, admin_user, _sheet(f"SLS-{suffix}", f"Sayfa Ürünü {suffix}", price="10.00", stock=1))
    assert sheet["slug"] == f"sayfa-urunu-{suffix}"
    try:
        body = {**sheet, "slug": f"Yeni-Adres-{suffix}"}
        resp = await client.put(f"/api/v1/admin/products/{sheet['id']}/sheet", headers=auth_header(admin_user), json=body)
        assert resp.status_code == 200, resp.text
        assert resp.json()["slug"] == f"yeni-adres-{suffix}"  # lowercased
        # The old address now finds the product under the new one.
        assert (await client.get(f"/api/v1/products/sayfa-urunu-{suffix}")).json()["slug"] == f"yeni-adres-{suffix}"

        bad = await client.put(
            f"/api/v1/admin/products/{sheet['id']}/sheet", headers=auth_header(admin_user), json={**body, "slug": "boş adres"}
        )
        assert bad.status_code == 422
        # Another product's address is refused.
        other = await _save(client, admin_user, _sheet(f"SLO-{suffix}", f"Diğer {suffix}", price="10.00", stock=1))
        clash = await client.put(
            f"/api/v1/admin/products/{other['id']}/sheet", headers=auth_header(admin_user),
            json={**other, "slug": f"yeni-adres-{suffix}"},
        )
        assert clash.status_code == 422 and "kullanılıyor" in clash.json()["detail"]
    finally:
        from app.services.product_blueprint import delete_product

        await db_session.rollback()
        from sqlalchemy.orm import selectinload

        found = await db_session.execute(
            select(Product).where(Product.base_code.like(f"SL%-{suffix}")).options(selectinload(Product.variants))
        )
        for product in found.scalars():
            await delete_product(db_session, product)
        await db_session.commit()


async def test_detail_lists_every_category(client: AsyncClient, db_session: AsyncSession, twins):
    from app.models.catalog import Category, ProductCategory

    first, _second, suffix = twins
    main = Category(name=f"Ana {suffix}", slug=f"ana-{suffix}")
    extra = Category(name=f"Ek {suffix}", slug=f"ek-{suffix}")
    db_session.add_all([main, extra])
    await db_session.flush()
    first.category_id = main.id
    db_session.add_all([
        ProductCategory(product_id=first.id, category_id=main.id, position=0),
        ProductCategory(product_id=first.id, category_id=extra.id, position=1),
    ])
    await db_session.commit()
    try:
        resp = await client.get(f"/api/v1/products/{first.slug}")
        assert resp.json()["category_ids"] == [main.id, extra.id]
    finally:
        await db_session.execute(delete(ProductCategory).where(ProductCategory.product_id == first.id))
        first.category_id = None
        await db_session.flush()
        await db_session.execute(delete(Category).where(Category.id.in_([main.id, extra.id])))
        await db_session.commit()


async def test_list_filters_by_ids(client: AsyncClient, twins):
    first, second, _ = twins
    resp = await client.get("/api/v1/products/", params={"ids": [str(first.id)]})
    assert [p["id"] for p in resp.json()] == [str(first.id)]
    both = await client.get("/api/v1/products/", params={"ids": [str(first.id), str(second.id)]})
    assert {p["id"] for p in both.json()} == {str(first.id), str(second.id)}
