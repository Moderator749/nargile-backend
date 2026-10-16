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
