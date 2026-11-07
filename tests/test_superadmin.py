"""The Sistem Yöneticisi (User.is_superadmin): only it manages the panel team,
and no other admin — a SUPER_ADMIN by role included — may touch its account."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import CustomerSegment, SegmentType, User, UserRole
from tests.conftest import auth_header

URL = "/api/v1/admin/users"


async def _user(db: AsyncSession, segments, *, role=UserRole.ADMIN, root=False, name="u") -> User:
    user = User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.com",
        username=f"{name}{uuid.uuid4().hex[:8]}",
        hashed_password="not-a-real-hash",
        role=role,
        is_superadmin=root,
        segment_id=segments[SegmentType.RETAIL].id,
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest.fixture
async def root(db_session: AsyncSession, segments: dict[SegmentType, CustomerSegment]) -> User:
    """The developer's account: a Sistem Yöneticisi."""
    return await _user(db_session, segments, role=UserRole.SUPER_ADMIN, root=True, name="root")


@pytest.fixture
async def member(db_session: AsyncSession, segments: dict[SegmentType, CustomerSegment]) -> User:
    return await _user(db_session, segments, role=UserRole.ADMIN, name="member")


# ── Only the Sistem Yöneticisi manages the team ──────────────────────────────


@pytest.mark.parametrize("role", [UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.STAFF, UserRole.CUSTOMER])
async def test_nobody_else_reaches_team_management(
    client: AsyncClient, db_session: AsyncSession, segments, member: User, role: UserRole
):
    """Not even a SUPER_ADMIN by role: being the Sistem Yöneticisi is the flag.
    To an admin the routes do not exist (404, as any unknown route); to staff
    and customers the admin panel itself is closed (403)."""
    caller = auth_header(await _user(db_session, segments, role=role, name="other"))
    body = {"email": "x@test.com", "password": "uzun-sifre-1", "role": "ADMIN"}
    refused = 404 if role in (UserRole.SUPER_ADMIN, UserRole.ADMIN) else 403
    for response in (
        await client.get(URL, headers=caller),
        await client.post(URL, headers=caller, json=body),
        await client.patch(f"{URL}/{member.id}", headers=caller, json={"role": "STAFF"}),
        await client.post(f"{URL}/{member.id}/password", headers=caller, json={"password": "uzun-sifre-1"}),
        await client.delete(f"{URL}/{member.id}", headers=caller),
    ):
        assert response.status_code == refused, response.text
    if refused == 404:
        assert (await client.get(URL, headers=caller)).json() == {"detail": "Not Found"}


async def test_the_sistem_yoneticisi_manages_the_team(
    client: AsyncClient, db_session: AsyncSession, root: User, member: User, customer_user: User
):
    h = auth_header(root)
    listed = (await client.get(URL, headers=h)).json()
    mine = next(m for m in listed if m["id"] == str(root.id))
    assert (mine["is_superadmin"], mine["is_me"]) == (True, True)
    assert listed[0]["is_superadmin"]  # the Sistem Yöneticisi first
    assert str(member.id) in {m["id"] for m in listed}
    assert str(customer_user.id) not in {m["id"] for m in listed}

    email = f"yeni-{uuid.uuid4().hex[:6]}@test.com"
    made = await client.post(
        URL, headers=h, json={"email": f"  {email.upper()} ", "full_name": " Yeni  Personel ", "role": "STAFF", "password": "uzun-sifre-1"}
    )
    assert made.status_code == 201, made.text
    assert (made.json()["email"], made.json()["full_name"], made.json()["role"], made.json()["is_superadmin"]) == (
        email, "Yeni Personel", "STAFF", False,
    )
    again = await client.post(URL, headers=h, json={"email": email, "password": "uzun-sifre-1"})
    assert again.status_code == 409
    taken = await client.post(URL, headers=h, json={"email": customer_user.email, "password": "uzun-sifre-1"})
    assert taken.status_code == 409  # a customer is not promoted here
    short = await client.post(URL, headers=h, json={"email": f"k-{email}", "password": "kisa"})
    assert short.status_code == 422

    promoted = await client.patch(f"{URL}/{made.json()['id']}", headers=h, json={"role": "ADMIN"})
    assert promoted.json()["role"] == "ADMIN"


async def test_deactivating_or_a_new_password_ends_a_members_sessions(
    client: AsyncClient, db_session: AsyncSession, root: User, member: User
):
    h, theirs = auth_header(root), auth_header(member)
    assert (await client.get("/api/v1/auth/me", headers=theirs)).status_code == 200

    assert (await client.post(f"{URL}/{member.id}/password", headers=h, json={"password": "yeni-sifre-1"})).status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=theirs)).status_code == 401

    await db_session.refresh(member)
    theirs = auth_header(member)
    off = await client.patch(f"{URL}/{member.id}", headers=h, json={"is_active": False})
    assert off.status_code == 200 and off.json()["is_active"] is False
    assert (await client.get("/api/v1/auth/me", headers=theirs)).status_code == 401


async def test_removing_a_member_keeps_the_row_off_the_panel(
    client: AsyncClient, db_session: AsyncSession, root: User, member: User
):
    h = auth_header(root)
    assert (await client.delete(f"{URL}/{member.id}", headers=h)).status_code == 204
    await db_session.refresh(member)
    assert (member.role, member.is_active) == (UserRole.CUSTOMER, False)
    assert str(member.id) not in {m["id"] for m in (await client.get(URL, headers=h)).json()}
    assert (await client.delete(f"{URL}/{member.id}", headers=h)).status_code == 404


async def test_the_sistem_yoneticisi_cannot_lock_itself_out(client: AsyncClient, root: User):
    h = auth_header(root)
    assert (await client.delete(f"{URL}/{root.id}", headers=h)).status_code == 409
    assert (await client.patch(f"{URL}/{root.id}", headers=h, json={"role": "STAFF"})).status_code == 409
    assert (await client.patch(f"{URL}/{root.id}", headers=h, json={"is_active": False})).status_code == 409
    assert (await client.post(f"{URL}/{root.id}/password", headers=h, json={"password": "uzun-sifre-1"})).status_code == 409
    # Its name is its own to change.
    renamed = await client.patch(f"{URL}/{root.id}", headers=h, json={"full_name": "Geliştirici"})
    assert renamed.status_code == 200 and renamed.json()["full_name"] == "Geliştirici"


async def test_another_sistem_yoneticisi_may_change_it(
    client: AsyncClient, db_session: AsyncSession, segments, root: User
):
    peer = await _user(db_session, segments, role=UserRole.SUPER_ADMIN, root=True, name="peer")
    changed = await client.patch(f"{URL}/{root.id}", headers=auth_header(peer), json={"role": "ADMIN"})
    assert changed.status_code == 200 and changed.json()["role"] == "ADMIN"


# ── The absolute guard on the account, wherever an admin can change it ──────


@pytest.mark.parametrize("role", [UserRole.SUPER_ADMIN, UserRole.ADMIN])
async def test_a_regular_admin_cannot_touch_the_sistem_yoneticisi(
    client: AsyncClient, db_session: AsyncSession, segments, root: User, role: UserRole
):
    """From the customers screen: its price list and its points are refused (403)."""
    h = auth_header(await _user(db_session, segments, role=role, name="admin"))
    segment = await client.put(f"/api/v1/admin/customers/{root.id}/segment", headers=h, json={"segment": "WHOLESALE"})
    assert segment.status_code == 403, segment.text
    points = await client.post(f"/api/v1/admin/customers/{root.id}/points", headers=h, json={"amount": 50, "note": "deneme"})
    assert points.status_code == 403, points.text
    assert "sistem" not in segment.text.lower()  # the refusal does not say why
    await db_session.refresh(root)
    assert root.segment_id == segments[SegmentType.RETAIL].id

    # Seen as an account like any: not flagged, only not theirs to change.
    detail = (await client.get(f"/api/v1/admin/customers/{root.id}", headers=h)).json()
    assert (detail["is_superadmin"], detail["can_manage"]) == (False, False)
    rows = (await client.get("/api/v1/admin/customers", headers=h, params={"q": root.email})).json()
    assert [r["is_superadmin"] for r in rows] == [False]


async def test_the_sistem_yoneticisi_changes_its_own_account(
    client: AsyncClient, db_session: AsyncSession, segments, root: User, member: User
):
    own = await client.put(f"/api/v1/admin/customers/{root.id}/segment", headers=auth_header(root), json={"segment": "WHOLESALE"})
    assert own.status_code == 200, own.text
    assert (own.json()["is_superadmin"], own.json()["can_manage"]) == (True, True)
    rows = (await client.get("/api/v1/admin/customers", headers=auth_header(root), params={"q": root.email})).json()
    assert [r["is_superadmin"] for r in rows] == [True]
    # And an ordinary admin's account stays any admin's to change.
    other = auth_header(await _user(db_session, segments, role=UserRole.ADMIN, name="admin"))
    assert (await client.put(f"/api/v1/admin/customers/{member.id}/segment", headers=other, json={"segment": "WHOLESALE"})).status_code == 200


async def test_me_says_who_is_the_sistem_yoneticisi(client: AsyncClient, root: User, member: User):
    assert (await client.get("/api/v1/auth/me", headers=auth_header(root))).json()["is_superadmin"] is True
    assert (await client.get("/api/v1/auth/me", headers=auth_header(member))).json()["is_superadmin"] is False


# ── scripts/set_superadmin.py ────────────────────────────────────────────────


async def test_the_script_promotes_an_admin_once(db_session: AsyncSession, segments, capsys):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    import scripts.set_superadmin as script

    # The test database's sessions, as the API's background work gets them.
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    admin = await _user(db_session, segments, role=UserRole.ADMIN, name="dev")
    customer = await _user(db_session, segments, role=UserRole.CUSTOMER, name="buyer")

    await script.run(admin.username.upper(), None, revoke=False, dry_run=True, sessions=sessions)
    await db_session.refresh(admin)
    assert admin.is_superadmin is False

    await script.run(admin.username, None, revoke=False, dry_run=False, sessions=sessions)
    await script.run(admin.username, None, revoke=False, dry_run=False, sessions=sessions)
    await db_session.refresh(admin)
    assert admin.is_superadmin is True
    assert "zaten sistem yöneticisi" in capsys.readouterr().out

    with pytest.raises(SystemExit, match="not an active admin"):
        await script.run(customer.username, None, revoke=False, dry_run=False, sessions=sessions)
