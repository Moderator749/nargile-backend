"""A product the shop no longer shows keeps somewhere to send its visitors:
GET /products/{ref}/shelf, which the storefront answers with a 308."""

import uuid

from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Category, Product, ProductCategory, SlugRedirect


async def _shelf(client: AsyncClient, ref: str):
    return await client.get(f"/api/v1/products/{ref}/shelf")


async def test_an_unpublished_or_deleted_product_leads_to_its_open_category(client: AsyncClient, db_session: AsyncSession):
    s = uuid.uuid4().hex[:6]
    group = Category(name=f"Grup {s}", slug=f"grup-{s}")
    db_session.add(group)
    await db_session.flush()
    hidden = Category(name=f"Gizli {s}", slug=f"gizli-{s}", parent_id=group.id, is_active=False)
    other = Category(name=f"Diğer {s}", slug=f"diger-{s}")
    db_session.add_all([hidden, other])
    await db_session.flush()
    unpublished = Product(base_code=f"U-{s}", slug=f"kapali-{s}", name=f"Kapalı {s}", category_id=group.id, is_published=False, tags=[])
    deleted = Product(base_code=f"D-{s}", slug=f"silinen-{s}", name=f"Silinen {s}", category_id=group.id, is_deleted=True, tags=[])
    # Its own category is switched off: the one above it, which is open.
    under_hidden = Product(base_code=f"H-{s}", slug=f"gizlide-{s}", name=f"Gizlide {s}", category_id=hidden.id, is_published=False, tags=[])
    live = Product(base_code=f"L-{s}", slug=f"satista-{s}", name=f"Satışta {s}", category_id=group.id, tags=[])
    db_session.add_all([unpublished, deleted, under_hidden, live])
    await db_session.flush()
    db_session.add(SlugRedirect(kind="product", old_slug=f"eski-adres-{s}", product_id=unpublished.id))
    await db_session.commit()
    try:
        for ref in (f"kapali-{s}", f"silinen-{s}", f"eski-adres-{s}", str(unpublished.id)):
            resp = await _shelf(client, ref)
            assert (resp.status_code, resp.json()) == (200, {"category_slug": f"grup-{s}"}), ref
        assert (await _shelf(client, f"gizlide-{s}")).json() == {"category_slug": f"grup-{s}"}
        # On sale: its own page answers, not this.
        assert (await _shelf(client, f"satista-{s}")).status_code == 404
        # An address no product ever had stays a true 404.
        assert (await _shelf(client, f"hic-olmamis-{s}")).status_code == 404
    finally:
        await db_session.execute(delete(SlugRedirect).where(SlugRedirect.old_slug == f"eski-adres-{s}"))
        await db_session.execute(delete(Product).where(Product.base_code.in_([f"U-{s}", f"D-{s}", f"H-{s}", f"L-{s}"])))
        await db_session.execute(delete(Category).where(Category.id.in_([hidden.id, other.id])))
        await db_session.execute(delete(Category).where(Category.id == group.id))
        await db_session.commit()


async def test_with_every_category_closed_another_listing_or_nothing(client: AsyncClient, db_session: AsyncSession):
    s = uuid.uuid4().hex[:6]
    closed = Category(name=f"Kapalı {s}", slug=f"kapali-kat-{s}", is_active=False)
    listed = Category(name=f"Listelendiği {s}", slug=f"listelendigi-{s}")
    db_session.add_all([closed, listed])
    await db_session.flush()
    elsewhere = Product(base_code=f"E-{s}", slug=f"baska-{s}", name=f"Başka {s}", category_id=closed.id, is_published=False, tags=[])
    nowhere = Product(base_code=f"N-{s}", slug=f"yersiz-{s}", name=f"Yersiz {s}", category_id=closed.id, is_published=False, tags=[])
    db_session.add_all([elsewhere, nowhere])
    await db_session.flush()
    db_session.add(ProductCategory(product_id=elsewhere.id, category_id=listed.id, position=0))
    await db_session.commit()
    try:
        assert (await _shelf(client, f"baska-{s}")).json() == {"category_slug": f"listelendigi-{s}"}
        assert (await _shelf(client, f"yersiz-{s}")).status_code == 404
    finally:
        await db_session.execute(delete(ProductCategory).where(ProductCategory.product_id == elsewhere.id))
        await db_session.execute(delete(Product).where(Product.id.in_([elsewhere.id, nowhere.id])))
        await db_session.execute(delete(Category).where(Category.id.in_([closed.id, listed.id])))
        await db_session.commit()
