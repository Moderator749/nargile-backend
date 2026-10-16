"""The landing popup ("Açılış duyurusu"): shown only while on and within its time range."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.site import POPUP_ID, PopupAnnouncement
from app.models.user import User
from tests.conftest import auth_header

ADMIN_URL = "/api/v1/admin/announcements/popup"
PUBLIC_URL = "/api/v1/announcements/popup"
FIELDS = ("title", "message", "is_active", "starts_at", "ends_at")


@pytest.fixture(autouse=True)
async def restore_popup(db_session: AsyncSession):
    """Snapshot the live popup and put it back, so tests never change the real site."""
    row = await db_session.get(PopupAnnouncement, POPUP_ID)
    saved = {f: getattr(row, f) for f in FIELDS} if row else None
    yield
    await db_session.rollback()
    if saved is None:
        await db_session.execute(delete(PopupAnnouncement).where(PopupAnnouncement.id == POPUP_ID))
    else:
        row = await db_session.get(PopupAnnouncement, POPUP_ID, populate_existing=True)
        for field, value in saved.items():
            setattr(row, field, value)
    await db_session.commit()


def _at(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


async def _save(client: AsyncClient, admin: User, **body):
    resp = await client.put(ADMIN_URL, headers=auth_header(admin), json={"message": "Kargo duyurusu", "is_active": True, **body})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_a_live_popup_is_shown_and_a_switched_off_one_is_not(client: AsyncClient, admin_user: User):
    saved = await _save(client, admin_user, title="  Bayram  ", message="  Bayram boyunca kargo bedava.  ")
    assert (saved["title"], saved["message"]) == ("Bayram", "Bayram boyunca kargo bedava.")  # trimmed
    public = (await client.get(PUBLIC_URL)).json()
    assert (public["title"], public["message"], public["updated_at"] is not None) == (
        "Bayram", "Bayram boyunca kargo bedava.", True
    )

    await _save(client, admin_user, is_active=False)
    assert (await client.get(PUBLIC_URL)).json() is None
    # The admin still sees the draft.
    draft = (await client.get(ADMIN_URL, headers=auth_header(admin_user))).json()
    assert (draft["message"], draft["is_active"]) == ("Kargo duyurusu", False)


@pytest.mark.parametrize(
    ("starts", "ends", "shown"),
    [
        (-1, 1, True),       # within the range
        (None, 1, True),     # no start: at once
        (-1, None, True),    # no end
        (1, 2, False),       # not started yet
        (-2, -1, False),     # over
    ],
)
async def test_the_popup_follows_its_time_range(
    client: AsyncClient, admin_user: User, starts: float | None, ends: float | None, shown: bool
):
    await _save(
        client, admin_user,
        starts_at=_at(starts) if starts is not None else None,
        ends_at=_at(ends) if ends is not None else None,
    )
    assert ((await client.get(PUBLIC_URL)).json() is not None) is shown


@pytest.mark.parametrize(
    "body",
    [
        {"message": "   ", "is_active": True},                                     # active needs text
        {"message": "x", "is_active": True, "starts_at": _at(2), "ends_at": _at(1)},  # end before start
        {"message": "x", "is_active": True, "ends_at": "2026-10-14T12:00:00"},        # no time zone
        {"message": "x" * 1001, "is_active": True},
        {"message": "x", "title": "t" * 81, "is_active": True},
    ],
)
async def test_impossible_popups_are_refused(client: AsyncClient, admin_user: User, body: dict):
    assert (await client.put(ADMIN_URL, headers=auth_header(admin_user), json=body)).status_code == 422


async def test_only_an_admin_edits_the_popup(client: AsyncClient, customer_user: User):
    body = {"message": "x", "is_active": False}
    assert (await client.put(ADMIN_URL, json=body)).status_code in (401, 403)
    assert (await client.put(ADMIN_URL, headers=auth_header(customer_user), json=body)).status_code == 403
    assert (await client.get(ADMIN_URL, headers=auth_header(customer_user))).status_code == 403
