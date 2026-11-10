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
    look = ("message", "is_active", "expires_at", "background_style", "bg_image_url", "text_color")
    saved = tuple(getattr(banner, f) for f in look) if banner else None
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
        for field, value in zip(look, saved):
            setattr(row, field, value)
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


async def _banner(client: AsyncClient, admin: User, message: str, active: bool = True, **look):
    resp = await client.put(
        "/api/v1/admin/announcement", headers=auth_header(admin),
        json={"message": message, "is_active": active, "background_style": "dark_luxury", **look},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


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


async def test_a_sales_preset_counts_down_to_its_end(client: AsyncClient, admin_user: User):
    """"Geri sayım" on: the banner ticks to the preset's end; off (a memorial day), no timer."""
    sale = await _preset(client, admin_user, "11.11", -1, 5, show_countdown=True)
    assert sale["show_countdown"] is True
    shown = (await client.get(ACTIVE)).json()
    assert (shown["show_countdown"], shown["expires_at"]) == (True, sale["ends_at"])

    h = auth_header(admin_user)
    await client.put(f"{URL}/{sale['id']}", headers=h, json={**sale, "show_countdown": False})
    assert (await client.get(ACTIVE)).json()["show_countdown"] is False


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
        {"message": "x", "background_style": "black"},
        {"message": "x", "background_style": "#12345"},
        {"message": "x", "text_color": "white"},
        {"message": "x", "text_color": "#fffffff"},
        {"message": "x", "title": "t" * 81},
        {"message": "x", "show_countdown": True, "starts_at": _at(1)},                      # a countdown to nothing
    ],
)
async def test_impossible_presets_are_refused(client: AsyncClient, admin_user: User, body: dict):
    assert (await client.post(URL, headers=auth_header(admin_user), json=body)).status_code == 422


async def test_only_an_admin_manages_presets(client: AsyncClient, customer_user: User):
    h = auth_header(customer_user)
    assert (await client.get(URL, headers=h)).status_code == 403
    assert (await client.post(URL, headers=h, json={"message": "x"})).status_code == 403
    assert (await client.delete(f"{URL}/1", headers=h)).status_code == 403


# ── Colours ──────────────────────────────────────────────────────────────────


async def test_a_future_preset_keeps_its_own_colours(client: AsyncClient, admin_user: User):
    """Dated ahead, a preset is saved with its look, and edited it keeps it."""
    made = await _preset(
        client, admin_user, "Anma", 24, 48, title="  Anma günü  ",
        background_style="#0A0A0A", text_color="#FFFFFF", icon_type="memorial",
    )
    assert (made["title"], made["background_style"], made["text_color"]) == ("Anma günü", "#0a0a0a", "#ffffff")

    h = auth_header(admin_user)
    edited = await client.put(f"{URL}/{made['id']}", headers=h, json={**made, "background_style": "dark_luxury", "text_color": ""})
    assert edited.status_code == 200, edited.text
    assert (edited.json()["background_style"], edited.json()["text_color"]) == ("dark_luxury", None)
    # Not yet: its day has not come.
    assert ((await client.get(ACTIVE)).json() or {}).get("message") != made["message"]


async def test_a_live_preset_is_drawn_in_its_own_colours(client: AsyncClient, admin_user: User):
    """Its background replaces the banner's, image and text colour included."""
    await _banner(
        client, admin_user, "Varsayılan", background_style="crimson_gradient",
        bg_image_url="https://example.com/desen.jpg", text_color="#fde68a",
    )
    await _preset(client, admin_user, "Anma", -1, 1, background_style="#000000", text_color="#ffffff")
    shown = (await client.get(ACTIVE)).json()
    assert (shown["background_style"], shown["text_color"], shown["bg_image_url"]) == ("#000000", "#ffffff", None)


async def test_a_preset_without_a_look_wears_the_banners(client: AsyncClient, admin_user: User):
    await _banner(client, admin_user, "Varsayılan", background_style="#7c2d12", text_color="#fde68a")
    plain = await _preset(client, admin_user, "Düz", -1, 1)
    shown = (await client.get(ACTIVE)).json()
    assert (shown["message"], shown["background_style"], shown["text_color"]) == (plain["message"], "#7c2d12", "#fde68a")

    # Only a text colour of its own: on the banner's background.
    h = auth_header(admin_user)
    await client.put(f"{URL}/{plain['id']}", headers=h, json={**plain, "text_color": "#ffffff"})
    shown = (await client.get(ACTIVE)).json()
    assert (shown["background_style"], shown["text_color"]) == ("#7c2d12", "#ffffff")


async def test_the_banner_keeps_a_text_colour(client: AsyncClient, admin_user: User):
    saved = await _banner(client, admin_user, "Varsayılan", text_color="#FDE68A")
    assert saved["text_color"] == "#fde68a"
    assert (await client.get(ACTIVE)).json()["text_color"] == "#fde68a"
    automatic = await _banner(client, admin_user, "Varsayılan", text_color="")
    assert automatic["text_color"] is None
    refused = await client.put(
        "/api/v1/admin/announcement", headers=auth_header(admin_user),
        json={"message": "x", "is_active": False, "text_color": "beyaz"},
    )
    assert refused.status_code == 422


# ── Special-day templates ────────────────────────────────────────────────────


async def test_the_templates_are_written_once(db_session: AsyncSession):
    """Added or adopted on the first run, nothing on the second; 10 Kasım is
    black, white and automatic for its whole day. Rolled back afterwards."""
    from sqlalchemy import select

    from app.services import banner_templates

    try:
        first = await banner_templates.apply(db_session)
        assert {c.title for c in first} == {t.title for t in banner_templates.TEMPLATES}
        second = await banner_templates.apply(db_session)
        assert all(c.action == "unchanged" for c in second), second

        anma = await db_session.scalar(select(BannerPreset).where(BannerPreset.title == "10 Kasım Atatürk'ü Anma Günü"))
        assert (anma.background_style, anma.text_color, anma.icon_type, anma.is_scheduled) == ("#000000", "#ffffff", "memorial", True)
        tr = banner_templates.TR
        assert anma.starts_at.astimezone(tr).isoformat() == "2026-11-10T00:00:00+03:00"
        assert anma.ends_at.astimezone(tr).isoformat() == "2026-11-10T23:59:59+03:00"
        # The migration's 10 Kasım was adopted, not doubled.
        assert len((await db_session.scalars(select(BannerPreset).where(BannerPreset.message.like("%10 Kasım%")))).all()) == 1
        # Every other template waits, off.
        others = (await db_session.scalars(select(BannerPreset).where(BannerPreset.title.is_not(None)))).all()
        assert [p.title for p in others if p.is_scheduled] == ["10 Kasım Atatürk'ü Anma Günü"]
        assert all(len(p.message) <= 200 and p.starts_at < p.ends_at for p in others)
        # The sales days count down; no memorial day does.
        counting = {p.title for p in others if p.show_countdown}
        assert counting == {"11.11 Alışveriş Festivali", "Hayırlı Cumalar", "Sevgililer Günü", "Anneler Günü", "Babalar Günü"}
        assert not any(p.show_countdown for p in others if p.icon_type == "memorial")
    finally:
        await db_session.rollback()


async def test_hayirli_cumalar_takes_efsane_cumas_place(db_session: AsyncSession):
    """The Black Friday preset already there is rewritten as Hayırlı Cumalar, not
    doubled; its switch stays the admin's. Rolled back afterwards."""
    from sqlalchemy import select

    from app.services import banner_templates

    try:
        await db_session.execute(delete(BannerPreset).where(BannerPreset.title.in_(["Hayırlı Cumalar", "Efsane Cuma (Black Friday)"])))
        old = BannerPreset(title="Efsane Cuma (Black Friday)", message="Efsane Cuma!", icon_type="campaign", is_scheduled=True)
        db_session.add(old)
        await db_session.flush()

        await banner_templates.apply(db_session)
        rows = (await db_session.scalars(select(BannerPreset).where(BannerPreset.title.in_(["Hayırlı Cumalar", "Efsane Cuma (Black Friday)"])))).all()
        assert [r.id for r in rows] == [old.id]
        assert (old.title, old.show_countdown, old.is_scheduled) == ("Hayırlı Cumalar", True, True)
        assert old.message.startswith("Hayırlı Cumalar!")
        assert old.starts_at.astimezone(banner_templates.TR).date().isoformat() == "2026-11-27"
    finally:
        await db_session.rollback()
