"""The admin's "Not Defteri": create, list, edit, delete — each admin only their own."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole
from tests.conftest import auth_header

URL = "/api/v1/admin/notes"


@pytest.fixture
async def other_admin(db_session: AsyncSession, admin_user: User) -> User:
    user = User(
        email=f"admin2-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="not-a-real-hash",
        role=UserRole.ADMIN,
        segment_id=admin_user.segment_id,
        is_active=True,
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def moderator(db_session: AsyncSession, admin_user: User) -> User:
    user = User(
        email=f"mod-{uuid.uuid4().hex[:8]}@test.com",
        username="Moderator",
        hashed_password="not-a-real-hash",
        role=UserRole.ADMIN,
        segment_id=admin_user.segment_id,
        is_active=True,
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def test_a_note_is_written_listed_edited_and_deleted(client: AsyncClient, admin_user: User):
    h = auth_header(admin_user)
    made = await client.post(URL, headers=h, json={"title": "  Tedarikçi  ", "body": "Salı günü ara.\n  - fiyat listesi\n\n"})
    assert made.status_code == 201, made.text
    note = made.json()
    # Title trimmed; the body keeps its lines and indentation, loses only trailing space.
    assert (note["title"], note["body"]) == ("Tedarikçi", "<p>Salı günü ara.<br>- fiyat listesi</p>")

    second = (await client.post(URL, headers=h, json={"body": "Başlıksız not"})).json()
    listed = (await client.get(URL, headers=h)).json()
    assert [n["id"] for n in listed][:2] == [second["id"], note["id"]]  # last edited first

    edited = await client.put(f"{URL}/{note['id']}", headers=h, json={"title": "Tedarikçi", "body": "Perşembe"})
    assert edited.status_code == 200
    assert edited.json()["body"] == "<p>Perşembe</p>"
    assert edited.json()["updated_at"] >= note["updated_at"]
    assert (await client.get(URL, headers=h)).json()[0]["id"] == note["id"]  # now the latest

    assert (await client.delete(f"{URL}/{note['id']}", headers=h)).status_code == 204
    assert (await client.delete(f"{URL}/{note['id']}", headers=h)).status_code == 404
    assert note["id"] not in [n["id"] for n in (await client.get(URL, headers=h)).json()]


async def test_notes_are_personal(client: AsyncClient, admin_user: User, other_admin: User):
    mine = (await client.post(URL, headers=auth_header(admin_user), json={"title": "Benim notum"})).json()
    theirs = auth_header(other_admin)
    assert mine["id"] not in [n["id"] for n in (await client.get(URL, headers=theirs)).json()]
    # Another admin's note is "not found", for reading it back, changing it or deleting it.
    assert (await client.put(f"{URL}/{mine['id']}", headers=theirs, json={"title": "x"})).status_code == 404
    assert (await client.delete(f"{URL}/{mine['id']}", headers=theirs)).status_code == 404


@pytest.mark.parametrize(
    "body",
    [{"title": "   ", "body": "  \n "}, {}, {"title": "t" * 121}, {"body": "b" * 100_001}],
)
async def test_impossible_notes_are_refused(client: AsyncClient, admin_user: User, body: dict):
    assert (await client.post(URL, headers=auth_header(admin_user), json=body)).status_code == 422


async def test_only_admins_have_a_notebook(client: AsyncClient, customer_user: User):
    assert (await client.get(URL)).status_code in (401, 403)
    assert (await client.get(URL, headers=auth_header(customer_user))).status_code == 403
    assert (await client.post(URL, headers=auth_header(customer_user), json={"title": "x"})).status_code == 403


async def test_a_locked_note_is_kept_until_it_is_unlocked(client: AsyncClient, admin_user: User):
    h = auth_header(admin_user)
    note = (await client.post(URL, headers=h, json={"title": "Kasa"})).json()
    assert (note["locked"], note["can_manage_lock"]) == (False, True)

    locked = await client.put(f"{URL}/{note['id']}/lock", headers=h, json={"locked": True})
    assert locked.status_code == 200, locked.text
    assert locked.json()["locked"] is True
    assert locked.json()["updated_at"] == note["updated_at"]  # a lock is not an edit

    refused = await client.delete(f"{URL}/{note['id']}", headers=h)
    assert refused.status_code == 409
    assert "kilitli" in refused.json()["detail"]

    assert (await client.put(f"{URL}/{note['id']}/lock", headers=h, json={"locked": False})).json()["locked"] is False
    assert (await client.delete(f"{URL}/{note['id']}", headers=h)).status_code == 204


async def test_a_bulk_delete_keeps_locked_and_others_notes(client: AsyncClient, admin_user: User, other_admin: User):
    h = auth_header(admin_user)
    a, b, c = [(await client.post(URL, headers=h, json={"title": t})).json()["id"] for t in ("a", "b", "c")]
    theirs = (await client.post(URL, headers=auth_header(other_admin), json={"title": "x"})).json()["id"]
    await client.put(f"{URL}/{b}/lock", headers=h, json={"locked": True})

    result = (await client.post(f"{URL}/delete", headers=h, json={"note_ids": [a, b, c, theirs]})).json()
    assert sorted(result["deleted"]) == sorted([a, c])
    assert set(result["refused"]) == {str(b), str(theirs)}
    assert "kilitli" in result["refused"][str(b)]
    assert [n["id"] for n in (await client.get(URL, headers=h)).json()] == [b]


async def test_only_the_locker_and_the_moderator_manage_a_lock(
    client: AsyncClient, admin_user: User, moderator: User
):
    owner, mod = auth_header(admin_user), auth_header(moderator)
    note = (await client.post(URL, headers=owner, json={"title": "Fiyatlar"})).json()

    # The Moderator sees every admin's notes, with the author, and locks this one.
    listed = {n["id"]: n for n in (await client.get(URL, headers=mod)).json()}
    assert listed[note["id"]]["own"] is False and listed[note["id"]]["author"]
    assert (await client.put(f"{URL}/{note['id']}/lock", headers=mod, json={"locked": True})).json()["locked"] is True

    # Its author no longer sees that lock, can't lift it, and can't delete the note.
    mine = next(n for n in (await client.get(URL, headers=owner)).json() if n["id"] == note["id"])
    assert (mine["locked"], mine["can_manage_lock"]) == (None, False)
    assert (await client.put(f"{URL}/{note['id']}/lock", headers=owner, json={"locked": False})).status_code == 403
    assert (await client.delete(f"{URL}/{note['id']}", headers=owner)).status_code == 409

    # The Moderator reads others' notes but doesn't edit or delete them; it unlocks.
    assert (await client.put(f"{URL}/{note['id']}", headers=mod, json={"title": "x"})).status_code == 404
    assert (await client.delete(f"{URL}/{note['id']}", headers=mod)).status_code == 404
    assert (await client.put(f"{URL}/{note['id']}/lock", headers=mod, json={"locked": False})).json()["locked"] is False
    assert (await client.delete(f"{URL}/{note['id']}", headers=owner)).status_code == 204


async def test_another_admin_never_reaches_a_note_or_its_lock(
    client: AsyncClient, admin_user: User, other_admin: User
):
    # A plain admin's own lock is theirs; another plain admin never sees the note at all.
    h = auth_header(admin_user)
    note = (await client.post(URL, headers=h, json={"title": "Kendi kilidim"})).json()
    await client.put(f"{URL}/{note['id']}/lock", headers=h, json={"locked": True})
    assert note["id"] not in [n["id"] for n in (await client.get(URL, headers=auth_header(other_admin))).json()]
    other = await client.put(f"{URL}/{note['id']}/lock", headers=auth_header(other_admin), json={"locked": False})
    assert other.status_code == 404
