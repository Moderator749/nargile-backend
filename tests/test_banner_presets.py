"""Banner presets ("Hazır metinler"): admin CRUD, and a scheduled one taking the
banner over within its range and handing it back afterwards."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.site import ANNOUNCEMENT_ID, BannerPreset, SiteAnnouncement
from app.models.user import User
from tests.conftest import auth_header

URL = "/api/v1/admin/banner-presets"
ACTIVE = "/api/v1/banners/active"
TAG = "[test-preset]"


@pytest.fixture(autouse=True)
async def isolate(db_session: AsyncSession):
    """Seeded presets are switched off for the test and restored; this test's
    own presets and the banner row are put back as they were."""
    scheduled = [p.id for p in (await db_session.execute(
        BannerPreset.__table__.select().where(BannerPreset.is_scheduled.is_(True))
    )).all()]
    await db_session.execute(BannerPreset.__table__.update().values(is_scheduled=False))
    banner = await db_session.get(SiteAnnouncement, ANNOUNCEMENT_ID)
    saved = (banner.message, banner.is_active, banner.expires_at) if banner else None
    await db_session.commit()
    yield
    await db_session.rollback()
    await db_session.execute(delete(BannerPreset).where(BannerPreset.message.like(f"%{TAG}%")))
    if scheduled:
        await db_session.execute(
            BannerPreset.__table__.update().where(BannerPreset.id.in_(scheduled)).values(is_scheduled=True)
        )
    if saved is None:
        await db_session.execute(delete(SiteAnnouncement).where(SiteAnnouncement.id == ANNOUNCEMENT_ID))
    else:
        row = await db_session.get(SiteAnnouncement, ANNOUNCEMENT_ID, populate_existing=True)
        row.message, row.is_active, row.expires_at = saved
    await db_session.commit()


def _at(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


async def _preset(client: AsyncClient, admin: User, text: str, starts: float | None, ends: float | None, on=True, **extra):
    body = {
        "message": f"{text} {TAG}",
        "starts_at": _at(starts) if starts is not None else None,
        "ends_at": _at(ends) if ends is not None else None,
        "is_scheduled": on,
        **extra,
    }
    resp = await client.post(URL, headers=auth_header(admin), json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _banner(client: AsyncClient, admin: User, message: str, active: bool = True):
    resp = await client.put(
        "/api/v1/admin/announcement", headers=auth_header(admin),
        json={"message": message, "is_active": active, "background_style": "dark_luxury"},
    )
    assert resp.status_code == 200, resp.text


async def test_presets_are_listed_edited_and_deleted(client: AsyncClient, admin_user: User):
    h = auth_header(admin_user)
    seeded = (await client.get(URL, headers=h)).json()
    assert any("29 Ekim" in p["message"] for p in seeded)  # the migration's texts

    made = await _preset(client, admin_user, "Hafta sonu", None, None, on=False, icon_type="campaign")
    assert (made["icon_type"], made["is_scheduled"], made["starts_at"]) == ("campaign", False, None)

    edited = await client.put(
        f"{URL}/{made['id']}", headers=h,
        json={"message": f"  Hafta sonu kampanyası {TAG} ", "icon_type": "none", "starts_at": _at(1), "ends_at": _at(5), "is_scheduled": True},
    )
    assert edited.status_code == 200, edited.text
    assert (edited.json()["message"], edited.json()["is_scheduled"]) == (f"Hafta sonu kampanyası {TAG}", True)

    assert (await client.delete(f"{URL}/{made['id']}", headers=h)).status_code == 204
    assert (await client.delete(f"{URL}/{made['id']}", headers=h)).status_code == 404
    assert all(p["id"] != made["id"] for p in (await client.get(URL, headers=h)).json())


async def test_a_scheduled_preset_takes_the_banner_over_within_its_range(client: AsyncClient, admin_user: User):
    await _banner(client, admin_user, "Elle yazılan duyuru")
    assert (await client.get(ACTIVE)).json()["message"] == "Elle yazılan duyuru"

    live = await _preset(client, admin_user, "Bayram", -1, 2, icon_type="memorial")
    shown = (await client.get(ACTIVE)).json()
    assert shown["message"] == live["message"]
    assert (shown["icon_type"], shown["is_active"], shown["show_countdown"]) == ("memorial", True, False)
    assert shown["expires_at"] == live["ends_at"]  # it hides itself at the end of its range
    assert shown["background_style"] == "dark_luxury"  # in the banner's own look

    # Switched off, the typed banner is back.
    h = auth_header(admin_user)
    await client.put(f"{URL}/{live['id']}", headers=h, json={**live, "is_scheduled": False})
    assert (await client.get(ACTIVE)).json()["message"] == "Elle yazılan duyuru"


@pytest.mark.parametrize(("starts", "ends"), [(1, 3), (-3, -1)])
async def test_a_preset_outside_its_range_is_not_shown(client: AsyncClient, admin_user: User, starts: int, ends: int):
    await _banner(client, admin_user, "Elle yazılan duyuru", active=False)
    await _preset(client, admin_user, "Henüz değil / bitti", starts, ends)
    assert (await client.get(ACTIVE)).json() is None


async def test_a_scheduled_preset_shows_even_with_the_banner_switched_off(client: AsyncClient, admin_user: User):
    await _banner(client, admin_user, "Taslak", active=False)
    await _preset(client, admin_user, "Planlı", -1, 1)
    assert (await client.get(ACTIVE)).json()["message"] == f"Planlı {TAG}"


async def test_of_two_overlapping_presets_the_later_start_wins(client: AsyncClient, admin_user: User):
    await _preset(client, admin_user, "Uzun dönem", -48, 48)
    await _preset(client, admin_user, "Bugüne özel", -1, 1)
    assert (await client.get(ACTIVE)).json()["message"] == f"Bugüne özel {TAG}"


async def test_the_banner_falls_back_to_the_default_when_a_preset_ends(client: AsyncClient, admin_user: User):
    """Default (no expiry) → a preset within its range → its range over → the default again."""
    await _banner(client, admin_user, "Varsayılan duyuru")  # no expires_at: unlimited
    default = (await client.get(ACTIVE)).json()
    assert (default["message"], default["expires_at"]) == ("Varsayılan duyuru", None)

    preset = await _preset(client, admin_user, "Bayram", -1, 1)
    assert (await client.get(ACTIVE)).json()["message"] == preset["message"]

    # The range runs out (moved into the past, as time would): the default is back.
    h = auth_header(admin_user)
    await client.put(f"{URL}/{preset['id']}", headers=h, json={**preset, "starts_at": _at(-3), "ends_at": _at(-1)})
    back = (await client.get(ACTIVE)).json()
    assert (back["message"], back["expires_at"]) == ("Varsayılan duyuru", None)

    # With the default switched off too, nothing: the storefront shows its welcome.
    await _banner(client, admin_user, "Varsayılan duyuru", active=False)
    assert (await client.get(ACTIVE)).json() is None


@pytest.mark.parametrize(
    "body",
    [
        {"message": "   "},
        {"message": "x" * 201},
        {"message": "x", "is_scheduled": True},                                           # no range
        {"message": "x", "is_scheduled": True, "starts_at": _at(1)},                        # no end
        {"message": "x", "starts_at": _at(2), "ends_at": _at(1)},                           # end first
        {"message": "x", "starts_at": "2026-10-14T10:00:00", "ends_at": _at(1)},             # no zone
        {"message": "x", "icon_type": "fireworks"},
    ],
)
async def test_impossible_presets_are_refused(client: AsyncClient, admin_user: User, body: dict):
    assert (await client.post(URL, headers=auth_header(admin_user), json=body)).status_code == 422


async def test_only_an_admin_manages_presets(client: AsyncClient, customer_user: User):
    h = auth_header(customer_user)
    assert (await client.get(URL, headers=h)).status_code == 403
    assert (await client.post(URL, headers=h, json={"message": "x"})).status_code == 403
    assert (await client.delete(f"{URL}/1", headers=h)).status_code == 403
