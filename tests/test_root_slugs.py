"""Products and categories share the storefront's root (/<slug>): one slug, one page."""

import uuid

from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Category, Product
from scripts.rewrite_root_links import rewrite, rewrite_value
from tests.conftest import auth_header


async def test_a_new_product_steps_past_a_categorys_slug(db_session: AsyncSession):
    suffix = uuid.uuid4().hex[:6]
    category = Category(name=f"Raf {suffix}", slug=f"raf-{suffix}")
    db_session.add(category)
    await db_session.commit()
    product = Product(base_code="", name=f"Raf {suffix}", tags=[])
    db_session.add(product)
    await db_session.commit()
    try:
        assert product.slug == f"raf-{suffix}-2"
    finally:
        await db_session.execute(delete(Product).where(Product.id == product.id))
        await db_session.execute(delete(Category).where(Category.id == category.id))
        await db_session.commit()


async def test_a_category_cannot_take_a_products_slug_nor_a_page(client: AsyncClient, admin_user, db_session: AsyncSession):
    suffix = uuid.uuid4().hex[:6]
    product = Product(base_code=f"RS-{suffix}", name=f"Ürün {suffix}", tags=[])
    db_session.add(product)
    await db_session.commit()
    try:
        for slug, word in ((product.slug, "bir ürünün"), ("blog", "sitenin kendi sayfalarından")):
            resp = await client.post(
                "/api/v1/admin/categories", headers=auth_header(admin_user), json={"name": "Deneme", "slug": slug}
            )
            assert resp.status_code == 409, resp.text
            assert word in resp.json()["detail"]
    finally:
        await db_session.execute(delete(Product).where(Product.id == product.id))
        await db_session.commit()


def test_content_links_move_to_the_root():
    assert rewrite('<a href="/catalog/luleler">x</a>') == '<a href="/luleler">x</a>'
    assert rewrite("https://www.nargilemalzemesi.com/catalog/komurler?sort=price-asc") == (
        "https://www.nargilemalzemesi.com/komurler?sort=price-asc"
    )
    assert rewrite("/product/mr.wood-nargile-takimi-prisma-zebrano#a") == "/mr.wood-nargile-takimi-prisma-zebrano#a"
    # Left alone: a product by id, the catalog's own search, the panel, another site.
    for kept in ("/product/3516b037-8442-4373-895c-d8ac271efce1", "/catalog", "/catalog?q=sultan", "/admin/catalog/x",
                 "https://example.com/catalog/x"):
        assert rewrite(kept) == kept
    assert rewrite_value({"top": [{"href": "/catalog/oduman"}]}) == {"top": [{"href": "/oduman"}]}
