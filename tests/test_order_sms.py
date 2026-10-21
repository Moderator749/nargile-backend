"""SMS Ayarları and the order SMS: "Sipariş alındı" and "Kargoya verildi"."""

import logging

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationLog
from app.models.settings import ShopSetting
from app.models.user import User
from app.services import sms_settings
from app.services.notify import drivers
from tests.conftest import ADDRESS, CONSENTS, auth_header

SETTINGS = "/api/v1/admin/communications/sms-settings"
ADDRESS_MOBILE = "905320000000"  # ADDRESS's "0532 000 00 00"


@pytest.fixture(autouse=True)
async def no_saved_sms_settings(db_session: AsyncSession):
    """Start and end without a panel account: a saved one would make Netgsm
    the provider for every other test."""
    await db_session.execute(delete(ShopSetting).where(ShopSetting.key == sms_settings.KEY))
    await db_session.commit()
    yield
    await db_session.rollback()
    await db_session.execute(delete(ShopSetting).where(ShopSetting.key == sms_settings.KEY))
    await db_session.commit()


def _form(**overrides) -> dict:
    return {
        "usercode": "",
        "header": "",
        "order_placed": {"enabled": True, "text": sms_settings.DEFAULT_TEMPLATES[sms_settings.ORDER_PLACED]},
        "order_shipped": {"enabled": True, "text": sms_settings.DEFAULT_TEMPLATES[sms_settings.ORDER_SHIPPED]},
        **overrides,
    }


async def _checkout(client: AsyncClient, user: User, variant, payment: str = "BANK_TRANSFER"):
    body = {
        **CONSENTS,
        "payment_method": payment,
        "items": [{"variant_id": str(variant.id), "quantity": 1}],
        "shipping_address": ADDRESS,
        "shipping_consent": True,
    }
    resp = await client.post("/api/v1/orders/checkout", headers=auth_header(user), json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _logs(db: AsyncSession, order_number: str, purpose: str) -> list[NotificationLog]:
    rows = (await db.scalars(select(NotificationLog).where(NotificationLog.purpose == purpose))).all()
    return [r for r in rows if (r.meta or {}).get("order_number") == order_number]


# ── The panel ────────────────────────────────────────────────────────────────


async def test_settings_start_from_the_defaults(client: AsyncClient, admin_user: User):
    got = (await client.get(SETTINGS, headers=auth_header(admin_user))).json()
    assert got["password_set"] is False and got["provider"] == "mock"
    assert got["order_placed"]["text"] == sms_settings.DEFAULT_TEMPLATES[sms_settings.ORDER_PLACED]
    assert got["placeholders"]["order_shipped"] == ["Name", "OrderNo", "Tracking", "Carrier"]


async def test_the_password_is_stored_encrypted_and_never_read_back(
    client: AsyncClient, admin_user: User, db_session: AsyncSession
):
    h = auth_header(admin_user)
    saved = await client.put(SETTINGS, headers=h, json=_form(usercode="8503089065", header="NARGILEMLZ", password="gizli-Sifre-9"))
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert (body["usercode"], body["header"], body["password_set"], body["provider"]) == (
        "8503089065", "NARGILEMLZ", True, "netgsm",
    )
    assert "gizli-Sifre-9" not in saved.text
    row = await db_session.get(ShopSetting, sms_settings.KEY, populate_existing=True)
    assert "gizli-Sifre-9" not in row.value

    # Saved again without a password: the stored one stays.
    kept = (await client.put(SETTINGS, headers=h, json=_form(usercode="8503089065", header="NARGILEMLZ"))).json()
    assert kept["password_set"] is True
    assert (await sms_settings.load(db_session)).password == "gizli-Sifre-9"

    cleared = (await client.put(SETTINGS, headers=h, json=_form(clear_password=True))).json()
    assert cleared["password_set"] is False and cleared["provider"] == "mock"


@pytest.mark.parametrize(
    "overrides",
    [
        {"order_shipped": {"enabled": True, "text": "Kargonuz yolda: {TakipNo}"}},  # not a placeholder
        {"order_placed": {"enabled": True, "text": "{Tracking} hazır"}},  # not known when an order is placed
        {"order_placed": {"enabled": True, "text": "   "}},
        {"header": "COK-UZUN-BASLIK"},
        {"usercode": "abc"},
    ],
)
async def test_the_form_is_held_to_its_rules(client: AsyncClient, admin_user: User, overrides: dict):
    resp = await client.put(SETTINGS, headers=auth_header(admin_user), json=_form(**overrides))
    assert resp.status_code == 422


async def test_only_an_admin_reads_the_settings(client: AsyncClient, customer_user: User):
    assert (await client.get(SETTINGS, headers=auth_header(customer_user))).status_code == 403


# ── The order messages ───────────────────────────────────────────────────────


async def test_a_transfer_order_is_texted_at_checkout(
    client: AsyncClient, customer_user: User, db_session: AsyncSession, catalog, caplog
):
    with caplog.at_level(logging.INFO, logger="notify"):
        order = await _checkout(client, customer_user, catalog[0])
    [log] = await _logs(db_session, order["order_number"], sms_settings.ORDER_PLACED)
    assert (log.recipient, log.status, log.provider) == (ADDRESS_MOBILE, "sent", "mock")
    assert f"{order['order_number']} numaralı siparişiniz alınmıştır" in caplog.text


async def test_a_card_order_waits_for_its_payment(
    client: AsyncClient, customer_user: User, db_session: AsyncSession, catalog
):
    order = await _checkout(client, customer_user, catalog[0], payment="CREDIT_CARD")
    assert await _logs(db_session, order["order_number"], sms_settings.ORDER_PLACED) == []


async def test_a_switched_off_message_is_not_sent(
    client: AsyncClient, admin_user: User, customer_user: User, db_session: AsyncSession, catalog
):
    off = _form(order_placed={"enabled": False, "text": "Siparişiniz alındı."})
    assert (await client.put(SETTINGS, headers=auth_header(admin_user), json=off)).status_code == 200
    order = await _checkout(client, customer_user, catalog[0])
    assert await _logs(db_session, order["order_number"], sms_settings.ORDER_PLACED) == []


async def test_shipping_texts_the_tracking_number(
    client: AsyncClient, admin_user: User, customer_user: User, db_session: AsyncSession, catalog, caplog
):
    h = auth_header(admin_user)
    template = _form(order_shipped={"enabled": True, "text": "Sayın {Name}, {OrderNo} {Carrier} ile yolda. Takip No: {Tracking}"})
    assert (await client.put(SETTINGS, headers=h, json=template)).status_code == 200
    order = await _checkout(client, customer_user, catalog[0])
    assert (await client.post(f"/api/v1/admin/orders/{order['id']}/approve", headers=h)).status_code == 200

    with caplog.at_level(logging.INFO, logger="notify"):
        shipped = await client.post(
            f"/api/v1/admin/orders/{order['id']}/ship", headers=h, json={"carrier": "YURTICI", "tracking_code": "112233445566"}
        )
    assert shipped.status_code == 200, shipped.text
    [log] = await _logs(db_session, order["order_number"], sms_settings.ORDER_SHIPPED)
    assert (log.recipient, log.status) == (ADDRESS_MOBILE, "sent")
    assert f"{order['order_number']} Yurtiçi Kargo ile yolda. Takip No: 112233445566" in caplog.text


# ── Netgsm ───────────────────────────────────────────────────────────────────


class _Reply:
    def __init__(self, status: int, body: dict):
        self.status_code, self._body = status, body
        self.headers = {"content-type": "application/json"}
        self.text = str(body)

    def json(self):
        return self._body


async def test_the_test_sms_goes_through_the_panels_netgsm_account(
    client: AsyncClient, admin_user: User, monkeypatch
):
    h = auth_header(admin_user)
    account = _form(usercode="8503089065", header="NARGILEMLZ", password="gizli-Sifre-9")
    assert (await client.put(SETTINGS, headers=h, json=account)).status_code == 200
    calls = []

    async def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Reply(200, {"code": "00", "jobid": "17377215342605050417149344"})

    monkeypatch.setattr(drivers, "_post", fake_post)
    result = await client.post(f"{SETTINGS}/test", headers=h, json={"phone": "0532 123 45 67"})
    assert result.json() == {"ok": True, "provider": "netgsm", "error": None}
    [(url, sent)] = calls
    assert url == drivers.NETGSM_URL and sent["auth"] == ("8503089065", "gizli-Sifre-9")
    assert sent["json"]["msgheader"] == "NARGILEMLZ"
    assert sent["json"]["messages"][0]["no"] == "5321234567"

    async def refused(url, **kwargs):
        return _Reply(200, {"code": "30", "description": "Geçersiz kullanıcı adı veya şifre"})

    monkeypatch.setattr(drivers, "_post", refused)
    failed = (await client.post(f"{SETTINGS}/test", headers=h, json={"phone": "05321234567"})).json()
    assert failed["ok"] is False and "30" in failed["error"]

    bad = await client.post(f"{SETTINGS}/test", headers=h, json={"phone": "0212 123 45 67"})
    assert bad.status_code == 422  # a landline cannot take an SMS
