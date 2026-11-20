"""IndexNow: an admin save announces the storefront pages it changed — the
product (in its live languages), the shelves and brand page listing it — in
one batched ping, and never when the shop has no key or runs without
background jobs."""

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.brand import Brand
from app.models.catalog import Category, Product, ProductCategory
from app.models.user import User
from app.services import indexnow
from tests.conftest import auth_header

SITE = "https://www.example-shop.test"


@pytest.fixture
def sent(monkeypatch) -> list[list[str]]:
    """IndexNow on, with no wait and a stand-in for the POST."""
    batches: list[list[str]] = []

    async def record(urls):
        batches.append(list(urls))

    monkeypatch.setattr(settings, "INDEXNOW_KEY", "k" * 32)
    monkeypatch.setattr(settings, "INDEXNOW_SITE_URL", SITE)
    monkeypatch.setattr(settings, "BACKGROUND_JOBS", True)
    monkeypatch.setattr(indexnow, "DEBOUNCE_SECONDS", 0)
    monkeypatch.setattr(indexnow, "send", record)
    indexnow._pending.clear()
    yield batches
    indexnow._pending.clear()


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0.01)


@pytest.fixture
async def shelf(db_session: AsyncSession):
    tag = uuid.uuid4().hex[:8]
    category = Category(
        name=f"Raf {tag}", slug=f"raf-{tag}", translations={"en": {"name": "Shelf"}, "ar": {"name": "رف", "draft": True}}
    )
    second = Category(name=f"Ikinci {tag}", slug=f"ikinci-{tag}")
    brand = Brand(name=f"Marka {tag}", slug=f"marka-{tag}", search_term=f"Marka {tag}")
    db_session.add_all([category, second, brand])
    await db_session.flush()
    product = Product(
        base_code=f"IN-{tag}", name=f"Urun {tag}", slug=f"urun-{tag}", category_id=category.id, brand=brand.name,
        translations={"en": {"name": "Product"}},
    )
    db_session.add(product)
    await db_session.flush()
    db_session.add(ProductCategory(product_id=product.id, category_id=second.id, position=0))
    ids = {"tag": tag, "category": category.id, "second": second.id, "brand": brand.id, "product": product.id}
    await db_session.commit()
    yield ids
    await db_session.rollback()
    await db_session.execute(delete(ProductCategory).where(ProductCategory.product_id == ids["product"]))
    await db_session.execute(delete(Product).where(Product.id == ids["product"]))
    await db_session.execute(delete(Category).where(Category.id.in_([ids["category"], ids["second"]])))
    await db_session.execute(delete(Brand).where(Brand.id == ids["brand"]))
    await db_session.commit()


def test_page_urls_put_turkish_at_the_root(monkeypatch):
    monkeypatch.setattr(settings, "INDEXNOW_SITE_URL", SITE + "/")
    assert indexnow.page_urls("mr-wood", ["tr", "en"]) == [f"{SITE}/mr-wood", f"{SITE}/en/mr-wood"]
    assert indexnow.page_urls(None) == []


async def test_product_urls_cover_its_page_shelves_and_brand(db_session: AsyncSession, shelf, monkeypatch):
    monkeypatch.setattr(settings, "INDEXNOW_SITE_URL", SITE)
    t = shelf["tag"]
    urls = await indexnow.product_urls(db_session, [shelf["product"]])
    assert sorted(urls) == sorted([
        f"{SITE}/urun-{t}", f"{SITE}/en/urun-{t}",
        # The Arabic shelf is a draft: not announced.
        f"{SITE}/raf-{t}", f"{SITE}/en/raf-{t}",
        f"{SITE}/ikinci-{t}",
        f"{SITE}/marka-{t}",
    ])


async def test_saves_close_together_go_out_in_one_ping(client: AsyncClient, admin_user: User, shelf, sent, monkeypatch):
    monkeypatch.setattr(indexnow, "DEBOUNCE_SECONDS", 0.05)
    indexnow.queue([f"{SITE}/elsewhere"])
    response = await client.patch(
        f"/api/v1/admin/products/{shelf['product']}/publish",
        json={"is_published": False},
        headers=auth_header(admin_user),
    )
    assert response.status_code == 200, response.text
    await asyncio.sleep(0.1)
    await _settle()
    assert len(sent) == 1
    assert f"{SITE}/urun-{shelf['tag']}" in sent[0] and f"{SITE}/elsewhere" in sent[0]


async def test_category_save_pings_its_page(client: AsyncClient, admin_user: User, shelf, sent):
    response = await client.put(
        f"/api/v1/admin/categories/{shelf['second']}",
        json={"name": f"Ikinci {shelf['tag']}", "slug": f"ikinci-{shelf['tag']}"},
        headers=auth_header(admin_user),
    )
    assert response.status_code == 200, response.text
    await _settle()
    assert sent == [[f"{SITE}/ikinci-{shelf['tag']}"]]


async def test_nothing_is_sent_without_a_key_or_background_jobs(client: AsyncClient, admin_user: User, shelf, sent, monkeypatch):
    monkeypatch.setattr(settings, "BACKGROUND_JOBS", False)
    await client.patch(
        f"/api/v1/admin/products/{shelf['product']}/publish", json={"is_published": False}, headers=auth_header(admin_user)
    )
    monkeypatch.setattr(settings, "BACKGROUND_JOBS", True)
    monkeypatch.setattr(settings, "INDEXNOW_KEY", None)
    await client.patch(
        f"/api/v1/admin/products/{shelf['product']}/publish", json={"is_published": True}, headers=auth_header(admin_user)
    )
    await _settle()
    assert sent == [] and not indexnow._pending
