"""Tests for admin panel read endpoints, pricing validation and the announcement banner."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.site import ANNOUNCEMENT_ID, SiteAnnouncement
from app.models.user import User
from tests.conftest import ADDRESS, CONSENTS, auth_header


BANNER_FIELDS = (
    "message", "is_active", "expires_at", "show_countdown", "background_style", "bg_image_url",
    "icon_type",
)


@pytest.fixture
async def restore_announcement(db_session: AsyncSession):
    """Snapshot the live banner and restore it, so tests never change the real site."""
    row = await db_session.get(SiteAnnouncement, ANNOUNCEMENT_ID)
    saved = {f: getattr(row, f) for f in BANNER_FIELDS} if row else None
    yield
    await db_session.rollback()
    if saved is None:
        await db_session.execute(delete(SiteAnnouncement).where(SiteAnnouncement.id == ANNOUNCEMENT_ID))
    else:
        row = await db_session.get(SiteAnnouncement, ANNOUNCEMENT_ID, populate_existing=True)
        for field, value in saved.items():
            setattr(row, field, value)
    await db_session.commit()


# ── Authorization ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/admin/products"),
        ("GET", "/api/v1/admin/segments"),
        ("GET", "/api/v1/admin/announcement"),
        ("PUT", "/api/v1/admin/announcement"),
    ],
)
async def test_admin_panel_endpoints_reject_customers(
    client: AsyncClient, customer_user: User, method: str, path: str
):
    anon = await client.request(method, path, json={"message": "x", "is_active": False})
    assert anon.status_code in (401, 403)
    customer = await client.request(
        method, path, headers=auth_header(customer_user), json={"message": "x", "is_active": False}
    )
    assert customer.status_code == 403


# ── Announcement banner ──────────────────────────────────────────────────────


async def test_announcement_lifecycle(
    client: AsyncClient, admin_user: User, restore_announcement
):
    text = f"23 Nisan Kutlu Olsun! {uuid.uuid4().hex[:6]}"
    put = await client.put(
        "/api/v1/admin/announcement",
        headers=auth_header(admin_user),
        json={"message": f"  {text}  ", "is_active": True},
    )
    assert put.status_code == 200
    assert put.json()["message"] == text  # trimmed

    public = await client.get("/api/v1/announcement")
    assert public.status_code == 200
    assert public.json()["message"] == text

    off = await client.put(
        "/api/v1/admin/announcement",
        headers=auth_header(admin_user),
        json={"message": text, "is_active": False},
    )
    assert off.status_code == 200
    assert (await client.get("/api/v1/announcement")).json() is None

    # The admin still sees the saved draft.
    settings = await client.get("/api/v1/admin/announcement", headers=auth_header(admin_user))
    assert settings.json() == {**settings.json(), "message": text, "is_active": False}


async def test_active_announcement_requires_message(
    client: AsyncClient, admin_user: User, restore_announcement
):
    resp = await client.put(
        "/api/v1/admin/announcement",
        headers=auth_header(admin_user),
        json={"message": "   ", "is_active": True},
    )
    assert resp.status_code == 422


async def test_announcement_message_length_capped(
    client: AsyncClient, admin_user: User, restore_announcement
):
    resp = await client.put(
        "/api/v1/admin/announcement",
        headers=auth_header(admin_user),
        json={"message": "x" * 201, "is_active": False},
    )
    assert resp.status_code == 422


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


async def _put_banner(client: AsyncClient, admin: User, **fields):
    body = {"message": "Cumhuriyet Bayramı Kutlu Olsun!", "is_active": True, **fields}
    return await client.put("/api/v1/admin/announcement", headers=auth_header(admin), json=body)


async def test_banner_visual_fields_round_trip(
    client: AsyncClient, admin_user: User, restore_announcement
):
    expires = _iso(timedelta(days=2))
    put = await _put_banner(
        client, admin_user,
        expires_at=expires, show_countdown=True,
        background_style="dark_luxury", bg_image_url="https://cdn.example.com/pattern.png",
    )
    assert put.status_code == 200, put.text

    for path in ("/api/v1/banners/active", "/api/v1/announcement"):
        body = (await client.get(path)).json()
        assert body["show_countdown"] is True
        assert body["background_style"] == "dark_luxury"
        assert body["bg_image_url"] == "https://cdn.example.com/pattern.png"
        assert datetime.fromisoformat(body["expires_at"]) == datetime.fromisoformat(expires)


async def test_banner_defaults_when_fields_omitted(
    client: AsyncClient, admin_user: User, restore_announcement
):
    put = await _put_banner(client, admin_user)
    assert put.status_code == 200
    body = put.json()
    assert body["expires_at"] is None
    assert body["show_countdown"] is False
    assert body["background_style"] == "crimson_gradient"
    assert body["bg_image_url"] is None
    assert body["icon_type"] == "celebration"


@pytest.mark.parametrize("icon", ["celebration", "memorial", "campaign", "none"])
async def test_banner_icon_round_trip(
    client: AsyncClient, admin_user: User, restore_announcement, icon: str
):
    assert (await _put_banner(client, admin_user, icon_type=icon)).status_code == 200
    assert (await client.get("/api/v1/banners/active")).json()["icon_type"] == icon


async def test_image_url_from_any_host_is_encoded_not_rejected(
    client: AsyncClient, admin_user: User, restore_announcement
):
    # Real-world URLs (e.g. Wikimedia) contain spaces and parentheses; a CSS url() breakout must stay inert.
    put = await _put_banner(
        client, admin_user, bg_image_url=' https://upload.example.org/Ankara (2).jpg") ;x'
    )
    assert put.status_code == 200, put.text
    assert put.json()["bg_image_url"] == "https://upload.example.org/Ankara%20%282%29.jpg%22%29%20;x"


async def test_expired_banner_is_hidden_from_public(
    client: AsyncClient, admin_user: User, restore_announcement
):
    put = await _put_banner(client, admin_user, expires_at=_iso(timedelta(seconds=-1)), show_countdown=True)
    assert put.status_code == 200

    assert (await client.get("/api/v1/banners/active")).json() is None
    assert (await client.get("/api/v1/announcement")).json() is None
    # The admin still sees the saved (expired) banner.
    settings = await client.get("/api/v1/admin/announcement", headers=auth_header(admin_user))
    assert settings.json()["is_active"] is True


async def test_banner_disappears_once_expiry_passes(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, restore_announcement
):
    assert (await _put_banner(client, admin_user, expires_at=_iso(timedelta(hours=1)))).status_code == 200
    assert (await client.get("/api/v1/banners/active")).json() is not None

    # Time passes: move the stored expiry into the past instead of sleeping.
    row = await db_session.get(SiteAnnouncement, ANNOUNCEMENT_ID)
    row.expires_at = datetime.now(timezone.utc) - timedelta(milliseconds=1)
    await db_session.commit()
    assert (await client.get("/api/v1/banners/active")).json() is None


def test_is_live_boundary():
    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    banner = SiteAnnouncement(message="x", is_active=True, expires_at=now)
    assert banner.is_live(now - timedelta(microseconds=1))
    assert not banner.is_live(now)  # expires exactly at expires_at
    assert SiteAnnouncement(message="x", is_active=True, expires_at=None).is_live(now)
    assert not SiteAnnouncement(message="", is_active=True).is_live(now)
    assert not SiteAnnouncement(message="x", is_active=False).is_live(now)


async def test_custom_hex_background_is_normalised(
    client: AsyncClient, admin_user: User, restore_announcement
):
    put = await _put_banner(client, admin_user, background_style="#A1B2C3")
    assert put.status_code == 200
    assert put.json()["background_style"] == "#a1b2c3"


async def test_blank_image_url_is_stored_as_null(
    client: AsyncClient, admin_user: User, restore_announcement
):
    put = await _put_banner(client, admin_user, bg_image_url="   ")
    assert put.status_code == 200
    assert put.json()["bg_image_url"] is None


@pytest.mark.parametrize(
    "fields",
    [
        {"show_countdown": True},  # countdown without an expiry
        {"expires_at": "2026-10-29T23:59:00"},  # naive datetime: ambiguous zone
        {"background_style": "neon_green"},
        {"background_style": "#fff"},
        {"background_style": "red; background:url(x)"},
        {"bg_image_url": "javascript:alert(1)"},
        {"icon_type": "skull"},
        {"icon_type": None},
        {"bg_image_url": "/relative/path.png"},
        {"bg_image_url": "https://x.com/" + "a" * 500},
    ],
)
async def test_banner_rejects_invalid_fields(
    client: AsyncClient, admin_user: User, restore_announcement, fields: dict
):
    resp = await _put_banner(client, admin_user, **fields)
    assert resp.status_code == 422


# ── Products, segments, orders ───────────────────────────────────────────────


async def test_admin_products_include_full_matrix_and_stock(
    client: AsyncClient, admin_user: User, catalog
):
    normal_v, _ = catalog
    resp = await client.get(
        "/api/v1/admin/products",
        headers=auth_header(admin_user),
        params={"q": normal_v.sku, "limit": 10},
    )
    assert resp.status_code == 200
    variants = [v for p in resp.json() for v in p["variants"] if v["id"] == str(normal_v.id)]
    assert len(variants) == 1
    v = variants[0]
    assert {p["price_incl_tax"] for p in v["prices"]} == {"5000.00"}
    assert {(i["warehouse_type"], i["quantity"]) for i in v["inventory"]} == {("RETAIL_STOCK", 500)}


async def test_admin_segments(client: AsyncClient, admin_user: User):
    resp = await client.get("/api/v1/admin/segments", headers=auth_header(admin_user))
    assert resp.status_code == 200
    assert {s["code"] for s in resp.json()} >= {"RETAIL", "CAFE", "WHOLESALE", "DEALER"}


async def test_admin_orders_include_customer(
    client: AsyncClient, admin_user: User, customer_user: User, catalog
):
    normal_v, _ = catalog
    created = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(customer_user),
        json={**CONSENTS, 
            "payment_method": "BANK_TRANSFER",
            "items": [{"variant_id": str(normal_v.id), "quantity": 1}],
            "shipping_address": ADDRESS,
        },
    )
    assert created.status_code == 201
    resp = await client.get(
        "/api/v1/admin/orders",
        headers=auth_header(admin_user),
        params={"status": "AWAITING_TRANSFER", "limit": 200},
    )
    order = next(o for o in resp.json() if o["id"] == created.json()["id"])
    assert order["customer_email"] == customer_user.email
    assert order["status"] == "AWAITING_TRANSFER"  # havale orders wait for the transfer


@pytest.mark.parametrize(
    "price",
    [
        {"price_excl_tax": "-1.00", "price_incl_tax": "0.00", "vat_rate": "20.00"},
        {"price_excl_tax": "100.00", "price_incl_tax": "90.00", "vat_rate": "20.00"},
        {"price_excl_tax": "100.00", "price_incl_tax": "120.00", "vat_rate": "150.00"},
    ],
)
async def test_pricing_update_rejects_invalid_prices(
    client: AsyncClient, admin_user: User, catalog, segments, price: dict
):
    normal_v, _ = catalog
    retail_id = next(s.id for code, s in segments.items() if code.value == "RETAIL")
    resp = await client.put(
        f"/api/v1/admin/variants/{normal_v.id}/pricing",
        headers=auth_header(admin_user),
        json={"prices": [{"segment_id": retail_id, **price}]},
    )
    assert resp.status_code == 422


async def test_approve_invalid_order_id_is_422(client: AsyncClient, admin_user: User):
    resp = await client.post("/api/v1/admin/orders/not-a-uuid/approve", headers=auth_header(admin_user))
    assert resp.status_code == 422


# ── Variant weight (coal free-shipping rule) ─────────────────────────────────


async def test_admin_sets_and_clears_variant_weight(
    client: AsyncClient, admin_user: User, customer_user: User, catalog
):
    _, coal_v = catalog
    url = f"/api/v1/admin/variants/{coal_v.id}"
    h = auth_header(admin_user)

    set_ = await client.patch(url, headers=h, json={"weight_kg": "1.250"})
    assert set_.status_code == 200, set_.text
    assert set_.json()["weight_kg"] == "1.250"

    listed = await client.get("/api/v1/admin/products", headers=h, params={"q": coal_v.sku})
    assert listed.json()[0]["variants"][0]["weight_kg"] == "1.250"

    assert (await client.patch(url, headers=h, json={"weight_kg": None})).json()["weight_kg"] is None
    for bad in ("0", "-1", "1000.5", "1.2345"):
        assert (await client.patch(url, headers=h, json={"weight_kg": bad})).status_code == 422
    assert (await client.patch(f"/api/v1/admin/variants/{uuid.uuid4()}", headers=h, json={"weight_kg": "1"})).status_code == 404
    assert (await client.patch(url, headers=auth_header(customer_user), json={"weight_kg": "1"})).status_code == 403


async def test_admin_products_report_total_for_pagination(client: AsyncClient, admin_user: User, catalog):
    normal_v, _ = catalog
    resp = await client.get(
        "/api/v1/admin/products", headers=auth_header(admin_user), params={"q": normal_v.sku, "limit": 1}
    )
    assert resp.headers["x-total-count"] == "1"
    everything = await client.get("/api/v1/admin/products", headers=auth_header(admin_user), params={"limit": 1})
    assert int(everything.headers["x-total-count"]) >= 2 and len(everything.json()) == 1
