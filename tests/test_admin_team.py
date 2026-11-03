"""Ekip Kanalı and Günün Notu.

Covers:
- Admins, super admins and staff share one channel; a customer is kept out
- The drawer's poll: `after_id` gives only what came since, oldest first, and
  says when more than a page came; `before_id` pages back through history
- Unread: only the others' messages, after the member's marker; the marker
  moves forward only, never past the newest message; posting reads all before
- Withdrawing: one's own, or any for a super admin; gone from every list and
  count, reported in `deleted_ids` so an open drawer drops it
- Blank and over-long messages refused; text kept exactly as typed
- The posting limit
- Günün Notu: everyone reads it, only a super admin writes or clears it

The suite shares one database, so each test works with members of its own and
measures from where the channel stood when it began.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/admin/team"


async def _member(db: AsyncSession, role: UserRole, name: str) -> User:
    user = User(
        email=f"ekip-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        full_name=name,
        role=role,
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _post(client: AsyncClient, user: User, body: str) -> dict:
    resp = await client.post(f"{BASE}/messages", headers=auth_header(user), json={"body": body})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _get(client: AsyncClient, user: User, path: str = "messages", **params) -> dict:
    resp = await client.get(f"{BASE}/{path}", headers=auth_header(user), params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_admins_super_admins_and_staff_share_one_channel(client, db_session, admin_user):
    staff = await _member(db_session, UserRole.STAFF, "Depo Ekibi")
    admin = await _member(db_session, UserRole.ADMIN, "Sipariş Masası")
    sent = await _post(client, staff, "ORD-20261103084112-A1B2C3 kargoya hazır")
    assert sent["sender_name"] == "Depo Ekibi" and sent["mine"] is True and sent["can_delete"] is True

    seen_by_admin = (await _get(client, admin, after_id=sent["id"] - 1))["messages"]
    assert [(m["id"], m["mine"], m["can_delete"]) for m in seen_by_admin] == [(sent["id"], False, False)]
    # A super admin may withdraw anyone's.
    seen_by_super = (await _get(client, admin_user, after_id=sent["id"] - 1))["messages"]
    assert seen_by_super[0]["can_delete"] is True


@pytest.mark.parametrize(
    "method, path, body",
    [("get", "messages", None), ("post", "messages", {"body": "merhaba"}), ("get", "unread", None), ("get", "daily-note", None)],
)
async def test_a_customer_is_kept_out(client, customer_user, method, path, body):
    resp = await getattr(client, method)(f"{BASE}/{path}", headers=auth_header(customer_user), **({"json": body} if body else {}))
    assert resp.status_code == 403


async def test_the_poll_gives_only_what_came_since_oldest_first(client, db_session):
    staff = await _member(db_session, UserRole.STAFF, "Poller")
    first, second, third = [await _post(client, staff, f"mesaj {i}") for i in range(3)]

    page = await _get(client, staff, after_id=first["id"])
    assert [m["id"] for m in page["messages"]] == [second["id"], third["id"]] and page["has_more"] is False
    assert (await _get(client, staff, after_id=third["id"]))["messages"] == []
    # More than a page came since: the drawer is told to ask again.
    overflow = await _get(client, staff, after_id=first["id"] - 1, limit=2)
    assert [m["id"] for m in overflow["messages"]] == [first["id"], second["id"]] and overflow["has_more"] is True


async def test_history_pages_back_with_before_id(client, db_session):
    staff = await _member(db_session, UserRole.STAFF, "Arşiv")
    ids = [(await _post(client, staff, f"eski {i}"))["id"] for i in range(5)]

    latest = await _get(client, staff, limit=2)
    assert [m["id"] for m in latest["messages"]] == ids[3:] and latest["has_more"] is True
    earlier = await _get(client, staff, before_id=ids[3], limit=2)
    assert [m["id"] for m in earlier["messages"]] == ids[1:3]


async def test_unread_counts_the_others_and_the_marker_only_moves_forward(client, db_session):
    staff = await _member(db_session, UserRole.STAFF, "Yazan")
    reader = await _member(db_session, UserRole.ADMIN, "Okuyan")
    before = (await _get(client, reader, "unread"))["count"]

    a = await _post(client, staff, "bir")
    b = await _post(client, staff, "iki")
    now = await _get(client, reader, "unread")
    assert now["count"] == before + 2 and now["latest_id"] == b["id"]
    # The writer's own messages are not news to them.
    assert (await _get(client, staff, "unread"))["count"] == 0

    read = await client.post(f"{BASE}/read", headers=auth_header(reader), json={"last_read_id": a["id"]})
    assert read.json()["count"] == 1 and read.json()["last_read_id"] == a["id"]
    # Backwards is ignored; beyond the newest is held to it.
    back = await client.post(f"{BASE}/read", headers=auth_header(reader), json={"last_read_id": 0})
    assert back.json()["last_read_id"] == a["id"]
    ahead = await client.post(f"{BASE}/read", headers=auth_header(reader), json={"last_read_id": b["id"] + 10_000})
    assert ahead.json() == {"count": 0, "latest_id": b["id"], "last_read_id": b["id"]}

    # Replying is reading: posting moves the marker to one's own message.
    c = await _post(client, staff, "üç")
    await _post(client, reader, "cevap")
    assert (await _get(client, reader, "unread"))["count"] == 0 and c["id"] < (await _get(client, reader, "unread"))["last_read_id"]


async def test_withdrawing_a_message(client, db_session, admin_user):
    staff = await _member(db_session, UserRole.STAFF, "Silen")
    admin = await _member(db_session, UserRole.ADMIN, "Diğer")
    # Someone who has only read: posting would move their marker past both.
    reader = await _member(db_session, UserRole.STAFF, "Sadece Okur")
    own = await _post(client, staff, "yanlış sipariş numarası")
    other = await _post(client, admin, "bunu süper yönetici siler")
    unread_before = (await _get(client, reader, "unread"))["count"]

    assert (await client.delete(f"{BASE}/messages/{other['id']}", headers=auth_header(staff))).status_code == 403
    assert (await client.delete(f"{BASE}/messages/{own['id']}", headers=auth_header(staff))).status_code == 204
    assert (await client.delete(f"{BASE}/messages/{own['id']}", headers=auth_header(staff))).status_code == 404
    assert (await client.delete(f"{BASE}/messages/{other['id']}", headers=auth_header(admin_user))).status_code == 204

    page = await _get(client, admin, after_id=own["id"] - 1)
    assert own["id"] not in [m["id"] for m in page["messages"]] and other["id"] not in [m["id"] for m in page["messages"]]
    assert {own["id"], other["id"]} <= set(page["deleted_ids"])
    # A withdrawn message is no longer unread news.
    assert (await _get(client, reader, "unread"))["count"] == unread_before - 2


@pytest.mark.parametrize("body", ["   \n ", "x" * 2001], ids=["blank", "too long"])
async def test_blank_and_over_long_messages_are_refused(client, db_session, body):
    staff = await _member(db_session, UserRole.STAFF, "Boş")
    resp = await client.post(f"{BASE}/messages", headers=auth_header(staff), json={"body": body})
    assert resp.status_code == 422


async def test_text_is_kept_exactly_as_typed(client, db_session):
    staff = await _member(db_session, UserRole.STAFF, "Ham")
    body = '<b>kalın</b> & "tırnak"\n#A1B2C3 · STEAM 13-1'
    sent = await _post(client, staff, f"  {body}  ")
    assert sent["body"] == body  # trimmed at the ends, nothing escaped or stripped inside


async def test_the_posting_limit(client, db_session):
    staff = await _member(db_session, UserRole.STAFF, "Hızlı")
    for i in range(10):
        await _post(client, staff, f"seri {i}")
    resp = await client.post(f"{BASE}/messages", headers=auth_header(staff), json={"body": "bir tane daha"})
    assert resp.status_code == 429 and "birkaç saniye" in resp.json()["detail"]


async def test_the_daily_note(client, db_session, admin_user):
    staff = await _member(db_session, UserRole.STAFF, "Okur")
    admin = await _member(db_session, UserRole.ADMIN, "Yönetici")
    resp = await client.put(f"{BASE}/daily-note", headers=auth_header(admin_user), json={"text": "  Bugün Yurtiçi 16:00'da alıyor.  "})
    assert resp.status_code == 200, resp.text

    note = await _get(client, staff, "daily-note")
    assert note["text"] == "Bugün Yurtiçi 16:00'da alıyor." and note["updated_by_name"] and note["updated_at"]
    for user in (admin, staff):
        refused = await client.put(f"{BASE}/daily-note", headers=auth_header(user), json={"text": "değiştir"})
        assert refused.status_code == 403
    assert (await _get(client, staff, "daily-note"))["text"] == "Bugün Yurtiçi 16:00'da alıyor."

    cleared = await client.put(f"{BASE}/daily-note", headers=auth_header(admin_user), json={"text": ""})
    assert cleared.json()["text"] is None and cleared.json()["updated_by_name"]  # who cleared it is kept
    too_long = await client.put(f"{BASE}/daily-note", headers=auth_header(admin_user), json={"text": "x" * 501})
    assert too_long.status_code == 422
