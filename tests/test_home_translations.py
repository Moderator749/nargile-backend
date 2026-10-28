"""The homepage's translation memory (English and Arabic)."""

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content import SiteContent
from app.models.user import User
from tests.conftest import auth_header


@pytest.fixture
async def cleanup(db_session: AsyncSession):
    # From no memory at all: the migrations seed a few defaults' translations
    # (d6a3f9b2e417), which these tests are not about.
    await db_session.execute(delete(SiteContent).where(SiteContent.key == "homepage_i18n"))
    await db_session.commit()
    yield
    await db_session.rollback()
    await db_session.execute(delete(SiteContent).where(SiteContent.key == "homepage_i18n"))
    await db_session.commit()


async def test_empty_then_round_trips_and_drops_blanks(client: AsyncClient, admin_user: User, cleanup):
    empty = (await client.get("/api/v1/home/translations")).json()
    assert empty["en"] == {} and empty["ar"] == {}

    body = {
        "en": {"Fırsatları Kaçırma": " Don't Miss the Deals ", "Markalarımız": ""},
        "ar": {"Fırsatları Kaçırma": "لا تفوّت العروض"},
    }
    saved = await client.put("/api/v1/admin/home/translations", headers=auth_header(admin_user), json=body)
    assert saved.status_code == 200, saved.text
    public = (await client.get("/api/v1/home/translations")).json()
    # Trimmed, and a blank translation is none at all.
    assert public["en"] == {"Fırsatları Kaçırma": "Don't Miss the Deals"}
    assert public["ar"] == {"Fırsatları Kaçırma": "لا تفوّت العروض"}
    assert public["updated_at"]


async def test_only_admins_write_and_only_known_languages(client: AsyncClient, admin_user: User, cleanup):
    assert (await client.put("/api/v1/admin/home/translations", json={"en": {}})).status_code == 401
    bad = await client.put("/api/v1/admin/home/translations", headers=auth_header(admin_user), json={"fr": {"a": "b"}})
    assert bad.status_code == 422
