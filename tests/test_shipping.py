"""Shipping: "Kargola" by hand and through Yurtiçi Kargo's web service.

The Yurtiçi answers below are the service's own, captured from its test
system (testws.yurticikargo.com, user YKTEST); the one for a scanned parcel
adds the item detail a processed shipment carries. No test reaches Yurtiçi:
the endpoints run against a stand-in provider.
"""

import logging
import uuid
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.order import Carrier, Order, ShippingPayer
from app.models.user import User
from app.services import shipping
from app.services.shipping import ShipmentStatus, ShippingError, yurtici
from app.services.shipping.sync import sync_shipments
from tests.conftest import auth_header
from tests.test_operations import _approve, _checkout

_HEAD = (
    "<env:Envelope xmlns:env='http://schemas.xmlsoap.org/soap/envelope/'><env:Header></env:Header><env:Body>"
)
_NS = (
    "xmlns:ns1='http://yurticikargo.com.tr/ShippingOrderDispatcherServices' "
    "xmlns:ns3='http://yurticikargo.com.tr/WSExceptions/'"
)
CREATED = (
    f"{_HEAD}<ns1:createShipmentResponse {_NS}><ShippingOrderResultVO><outFlag>0</outFlag><outResult>Başarılı</outResult>"
    "<count>1</count><jobId>2198836</jobId><shippingOrderDetailVO><cargoKey>NMTEST56315661</cargoKey>"
    "<invoiceKey>NMTEST56315661</invoiceKey></shippingOrderDetailVO></ShippingOrderResultVO>"
    "</ns1:createShipmentResponse></env:Body></env:Envelope>"
)
BAD_LOGIN = (
    f"{_HEAD}<ns1:createShipmentResponse {_NS}><ShippingOrderResultVO><outFlag>1</outFlag>"
    "<outResult>Web servis kullanıcı ve parola bilgileri örtüşmüyor.</outResult><errCode>1626</errCode>"
    "<count>0</count><jobId>0</jobId></ShippingOrderResultVO></ns1:createShipmentResponse></env:Body></env:Envelope>"
)
NOT_COLLECTED = (
    f"{_HEAD}<ns1:queryShipmentResponse {_NS}><ShippingDeliveryVO><outFlag>0</outFlag><outResult>Başarılı</outResult>"
    "<count>2</count><senderCustId>1010954</senderCustId><shippingDeliveryDetailVO><cargoKey>NMTEST56315661</cargoKey>"
    "<invoiceKey>NMTEST56315661</invoiceKey><jobId>2198836</jobId><operationCode>0</operationCode>"
    "<operationMessage>Kargo İşlem Görmemiş.</operationMessage><operationStatus>NOP</operationStatus>"
    "</shippingDeliveryDetailVO><shippingDeliveryDetailVO><cargoKey>NOSUCHKEY999</cargoKey><errCode>82519</errCode>"
    "<errMessage>YKTEST kullanıcısına ait NOSUCHKEY999 kargo anahtarı bulunmamaktadır.</errMessage><jobId>0</jobId>"
    "</shippingDeliveryDetailVO></ShippingDeliveryVO></ns1:queryShipmentResponse></env:Body></env:Envelope>"
)
IN_DELIVERY = (
    f"{_HEAD}<ns1:queryShipmentResponse {_NS}><ShippingDeliveryVO><outFlag>0</outFlag><outResult>Başarılı</outResult>"
    "<count>1</count><shippingDeliveryDetailVO><cargoKey>NMTEST56315661</cargoKey><operationCode>1</operationCode>"
    "<operationMessage>Kargo teslimattadır.</operationMessage><operationStatus>IND</operationStatus>"
    "<shippingDeliveryItemDetailVO><cargoKey>NMTEST56315661</cargoKey><docId>112233445566</docId>"
    "<trackingUrl>https://www.yurticikargo.com/Tracking/Detail/112233445566</trackingUrl>"
    "<cargoEventExplanation>Kargo dağıtıma çıktı</cargoEventExplanation></shippingDeliveryItemDetailVO>"
    "</shippingDeliveryDetailVO></ShippingDeliveryVO></ns1:queryShipmentResponse></env:Body></env:Envelope>"
)
CANCELLED = (
    f"{_HEAD}<ns1:cancelShipmentResponse {_NS}><ShippingOrderResultVO><outFlag>0</outFlag><outResult>Başarılı</outResult>"
    "<count>1</count><shippingCancelDetailVO><cargoKey>NMTEST56315661</cargoKey><docId>0</docId>"
    "<operationCode>3</operationCode><operationMessage>Verisi İptal Edilmiştir.Kargo Çıkışı Engellendi.</operationMessage>"
    "<operationStatus>CNL</operationStatus></shippingCancelDetailVO></ShippingOrderResultVO>"
    "</ns1:cancelShipmentResponse></env:Body></env:Envelope>"
)


# ── The Yurtiçi web service: requests and answers ────────────────────────────


def test_a_shipment_request_carries_the_parcel_escaped():
    body = yurtici.create_body(
        "U", "P&W", "TR",
        shipping.ShipmentRequest(
            cargo_key="K1", receiver_name="Ali <Veli>", address="Cad. No:3 & 5", city="İstanbul",
            district="Fatih", phone="05551112233",
        ),
    )
    assert "<cargoKey>K1</cargoKey><invoiceKey>K1</invoiceKey>" in body
    assert "<wsPassword>P&amp;W</wsPassword>" in body
    assert "<receiverCustName>Ali &lt;Veli&gt;</receiverCustName>" in body
    assert "<cityName>İstanbul</cityName><townName>Fatih</townName>" in body
    assert "<emailAddress>" not in body  # empty fields are left out
    # Our reference as its "Sipariş No", which the self-service report finds it by.
    assert "<specialField1>3$K1#</specialField1>" in body


def test_the_answers_are_read():
    yurtici.parse_create(CREATED, "NMTEST56315661")
    with pytest.raises(ShippingError, match="parola bilgileri örtüşmüyor"):
        yurtici.parse_create(BAD_LOGIN, "NMTEST56315661")

    waiting, unknown = yurtici.parse_query(NOT_COLLECTED)
    assert (waiting.code, waiting.text, waiting.tracking_number) == ("NOP", "Kargo İşlem Görmemiş.", None)
    assert unknown.error and "bulunmamaktadır" in unknown.error

    [moving] = yurtici.parse_query(IN_DELIVERY)
    assert (moving.code, moving.text, moving.tracking_number) == ("IND", "Kargo dağıtıma çıktı", "112233445566")
    assert moving.tracking_url == "https://www.yurticikargo.com/Tracking/Detail/112233445566"

    withdrawn = yurtici.parse_cancel(CANCELLED, "NMTEST56315661")
    assert withdrawn.cancelled and withdrawn.tracking_number is None


def test_cargo_keys_and_tracking_pages():
    order = Order(order_number="ORD-20261019085034-9E23B8")
    assert shipping.next_cargo_key(order) == "202610190850349E23B8"
    order.cargo_key = "202610190850349E23B8"
    assert shipping.next_cargo_key(order) == "202610190850349E23B8B"
    order.cargo_key = "202610190850349E23B8B"
    assert shipping.next_cargo_key(order) == "202610190850349E23B8C"

    assert shipping.tracking_url(Carrier.YURTICI, " 1234 ") == "https://www.yurticikargo.com/Tracking/Detail/1234"
    assert shipping.tracking_url(Carrier.OTHER, "1234") is None
    assert shipping.tracking_url(Carrier.YURTICI, "") is None


def test_the_reference_report_links_a_shipment_by_order_number_and_date():
    url = yurtici.reference_tracking_url("349063893", "202610190850349E23B8", date(2026, 10, 19))
    assert url == (
        "http://selfservis.yurticikargo.com/reports/SswReportsFromParamFields.aspx"
        "?ssfldvn=3&sskurkod=349063893&refnumber=202610190850349E23B8&date=19.10.2026"
    )
    assert yurtici.is_reference_url(url) and not yurtici.is_reference_url(yurtici.tracking_url("1"))
    assert yurtici.service_url(yurtici.LIVE_URL + "?wsdl") == yurtici.LIVE_URL


@pytest.fixture
def yurtici_accounts(monkeypatch):
    for name, value in {
        "YURTICI_MUSTERI_KODU": "349063893",
        "YURTICI_API_USER_GO": "GO-USER", "YURTICI_API_PASS_GO": "GO-PASS",
        "YURTICI_API_USER_AO": "AO-USER", "YURTICI_API_PASS_AO": "AO-PASS",
        "YURTICI_TEST_MODE": False,
    }.items():
        monkeypatch.setattr(settings, name, value)


def test_the_payer_picks_the_account(yurtici_accounts, monkeypatch):
    shop = shipping.provider(Carrier.YURTICI, ShippingPayer.SHOP)
    buyer = shipping.provider(Carrier.YURTICI, ShippingPayer.RECEIVER)
    assert (shop.user, shop.password, shop.url) == ("GO-USER", "GO-PASS", yurtici.LIVE_URL)
    assert (buyer.user, buyer.password) == ("AO-USER", "AO-PASS")
    assert shipping.provider(Carrier.OTHER, ShippingPayer.SHOP) is None

    # Test mode goes to Yurtiçi's test service, whatever the WSDL says.
    monkeypatch.setattr(settings, "YURTICI_TEST_MODE", True)
    assert shipping.provider(Carrier.YURTICI).url == yurtici.TEST_URL

    # No AO account: a karşı ödemeli parcel is not put on the shop's.
    monkeypatch.setattr(settings, "YURTICI_API_USER_AO", "")
    assert shipping.provider(Carrier.YURTICI, ShippingPayer.RECEIVER) is None


def test_the_names_before_the_split_still_count_as_the_go_account(monkeypatch):
    monkeypatch.setattr(settings, "YURTICI_API_USER", "OLD-USER")
    monkeypatch.setattr(settings, "YURTICI_API_PASSWORD", "OLD-PASS")
    monkeypatch.setattr(settings, "YURTICI_CUSTOMER_CODE", "111")
    assert settings.yurtici_credentials(receiver_pays=False) == ("OLD-USER", "OLD-PASS")
    assert settings.yurtici_credentials(receiver_pays=True) is None
    assert settings.yurtici_customer_code == "111"


# ── "Kargola" ────────────────────────────────────────────────────────────────


class FakeYurtici:
    """Stands in for Yurtiçi's web service; `statuses` is what it will report."""

    def __init__(self):
        # Each call as (operation, the payer whose account it was made on).
        self.payer = ShippingPayer.SHOP
        self.calls: list[tuple[str, ShippingPayer]] = []
        self.created: list[shipping.ShipmentRequest] = []
        self.cancelled: list[str] = []
        self.statuses: dict[str, ShipmentStatus] = {}
        self.fail: str | None = None

    async def create(self, shipment):
        self.calls.append(("create", self.payer))
        if self.fail:
            raise ShippingError(self.fail)
        self.created.append(shipment)

    async def query(self, cargo_keys):
        self.calls.append(("query", self.payer))
        return [self.statuses.get(k, ShipmentStatus(cargo_key=k, code="NOP", text="Kargo İşlem Görmemiş.")) for k in cargo_keys]

    async def cancel(self, cargo_key):
        self.calls.append(("cancel", self.payer))
        self.cancelled.append(cargo_key)
        return ShipmentStatus(cargo_key=cargo_key, code="CNL", text="Verisi İptal Edilmiştir.", cancelled=True)


@pytest.fixture
def fake_yurtici(monkeypatch):
    fake = FakeYurtici()
    def provider(carrier, payer=ShippingPayer.SHOP):
        if carrier != Carrier.YURTICI:
            return None
        fake.payer = ShippingPayer(payer)
        return fake

    monkeypatch.setattr(shipping, "provider", provider)
    monkeypatch.setattr(settings, "YURTICI_MUSTERI_KODU", "349063893")
    return fake


async def _paid_order(client, admin, customer, variant) -> dict:
    order = (await _checkout(client, customer, variant)).json()
    assert (await _approve(client, admin, order["id"])).status_code == 200
    return order


async def test_a_typed_yurtici_code_links_to_its_tracking_page(
    client: AsyncClient, admin_user: User, customer_user: User, catalog, caplog
):
    order = await _paid_order(client, admin_user, customer_user, catalog[0])
    with caplog.at_level(logging.INFO, logger="app.services.mailer"):
        resp = await client.post(
            f"/api/v1/admin/orders/{order['id']}/ship",
            headers=auth_header(admin_user),
            json={"carrier": "YURTICI", "tracking_code": "112233445566"},
        )
    assert resp.status_code == 200, resp.text
    shipped = resp.json()
    assert (shipped["status"], shipped["carrier"], shipped["tracking_code"]) == ("SHIPPED", "YURTICI", "112233445566")
    assert shipped["tracking_url"] == "https://www.yurticikargo.com/Tracking/Detail/112233445566"
    assert shipped["shipped_at"] is not None and shipped["cargo_key"] is None

    mail = "\n".join(r.getMessage() for r in caplog.records)
    assert "Kargo Firması: Yurtiçi Kargo" in mail
    assert "https://www.yurticikargo.com/Tracking/Detail/112233445566" in mail

    # The customer reads the same on their own order.
    mine = await client.get(f"/api/v1/orders/{order['id']}", headers=auth_header(customer_user))
    assert mine.json()["tracking_url"] == shipped["tracking_url"]


async def test_an_empty_body_ships_with_the_chosen_carrier_and_no_code(
    client: AsyncClient, admin_user: User, customer_user: User, catalog
):
    order = await _paid_order(client, admin_user, customer_user, catalog[0])
    resp = await client.post(f"/api/v1/admin/orders/{order['id']}/ship", headers=auth_header(admin_user), json={})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["carrier"], resp.json()["tracking_code"], resp.json()["tracking_url"]) == ("YURTICI", None, None)


async def test_another_carrier_is_named_and_the_van_has_nothing_to_track(
    client: AsyncClient, admin_user: User, customer_user: User, catalog
):
    other = await _paid_order(client, admin_user, customer_user, catalog[0])
    resp = await client.post(
        f"/api/v1/admin/orders/{other['id']}/ship",
        headers=auth_header(admin_user),
        json={"carrier": "OTHER", "carrier_name": "Aras Kargo", "tracking_code": "AR-9"},
    )
    assert (resp.json()["carrier"], resp.json()["carrier_name"], resp.json()["tracking_url"]) == ("OTHER", "Aras Kargo", None)

    van = await _paid_order(client, admin_user, customer_user, catalog[0])
    resp = await client.post(
        f"/api/v1/admin/orders/{van['id']}/ship", headers=auth_header(admin_user), json={"carrier": "OWN", "carrier_name": "x"}
    )
    assert (resp.json()["carrier"], resp.json()["carrier_name"]) == ("OWN", None)


async def test_the_web_service_is_offered_only_when_configured(
    client: AsyncClient, admin_user: User, customer_user: User, catalog
):
    order = await _paid_order(client, admin_user, customer_user, catalog[0])
    integrations = await client.get("/api/v1/admin/shipping/integrations", headers=auth_header(admin_user))
    assert integrations.json()["yurtici_api"] is False  # no credentials in the test settings
    resp = await client.post(
        f"/api/v1/admin/orders/{order['id']}/ship", headers=auth_header(admin_user), json={"carrier": "YURTICI", "use_api": True}
    )
    assert resp.status_code == 409
    assert (await client.get("/api/v1/admin/shipping/integrations", headers=auth_header(customer_user))).status_code == 403


async def test_a_web_service_shipment_is_registered_then_tracked(
    client: AsyncClient, admin_user: User, customer_user: User, catalog, fake_yurtici: FakeYurtici, db_session: AsyncSession
):
    order = await _paid_order(client, admin_user, customer_user, catalog[0])
    h = auth_header(admin_user)
    resp = await client.post(f"/api/v1/admin/orders/{order['id']}/ship", headers=h, json={"carrier": "YURTICI", "use_api": True})
    assert resp.status_code == 200, resp.text
    shipped = resp.json()
    key = shipping.next_cargo_key(Order(order_number=order["order_number"]))
    assert (shipped["status"], shipped["cargo_key"], shipped["carrier_status"]) == ("SHIPPED", key, "NOP")
    assert shipped["tracking_code"] is None  # Yurtiçi's number comes once the label is scanned
    # Followed by our reference until then, on the shop's own account.
    assert shipped["tracking_url"].startswith(
        f"http://selfservis.yurticikargo.com/reports/SswReportsFromParamFields.aspx?ssfldvn=3&sskurkod=349063893&refnumber={key}&date="
    )
    assert fake_yurtici.calls == [("create", ShippingPayer.SHOP)]

    [sent] = fake_yurtici.created
    assert (sent.cargo_key, sent.city, sent.district) == (key, order["shipping_address"]["city"], order["shipping_address"]["district"])
    assert sent.receiver_name == order["shipping_address"]["full_name"]

    # The branch scans it: the hourly sync brings Yurtiçi's number and page.
    fake_yurtici.statuses[key] = ShipmentStatus(
        cargo_key=key, code="IND", text="Kargo dağıtıma çıktı", tracking_number="112233445566",
        tracking_url="https://www.yurticikargo.com/Tracking/Detail/112233445566",
    )
    assert await sync_shipments(db_session) == 1
    tracked = (await client.get(f"/api/v1/admin/orders/{order['id']}", headers=h)).json()
    assert (tracked["tracking_code"], tracked["carrier_status"], tracked["carrier_status_text"]) == (
        "112233445566", "IND", "Kargo dağıtıma çıktı",
    )
    assert tracked["tracking_url"] == "https://www.yurticikargo.com/Tracking/Detail/112233445566"

    # Collected: it can no longer be withdrawn from here.
    assert (await client.post(f"/api/v1/admin/orders/{order['id']}/shipment/cancel", headers=h)).status_code == 409

    # Delivered: refreshed on demand, and the sync leaves it alone after.
    fake_yurtici.statuses[key] = ShipmentStatus(cargo_key=key, code="DLV", text="Teslim edildi", delivered=True)
    refreshed = await client.post(f"/api/v1/admin/orders/{order['id']}/shipment/refresh", headers=h)
    assert refreshed.json()["carrier_status"] == "DLV"
    assert await sync_shipments(db_session) == 0


async def test_an_uncollected_shipment_is_withdrawn_and_can_ship_again(
    client: AsyncClient, admin_user: User, customer_user: User, catalog, fake_yurtici: FakeYurtici
):
    order = await _paid_order(client, admin_user, customer_user, catalog[0])
    h = auth_header(admin_user)
    url = f"/api/v1/admin/orders/{order['id']}"
    first = (await client.post(f"{url}/ship", headers=h, json={"carrier": "YURTICI", "use_api": True})).json()["cargo_key"]

    back = await client.post(f"{url}/shipment/cancel", headers=h)
    assert back.status_code == 200, back.text
    assert (back.json()["status"], back.json()["carrier_status"], back.json()["shipped_at"]) == ("APPROVED", "CNL", None)
    assert fake_yurtici.cancelled == [first]

    # Registered again under a new key: Yurtiçi keeps the first one as cancelled.
    again = (await client.post(f"{url}/ship", headers=h, json={"carrier": "YURTICI", "use_api": True})).json()
    assert again["status"] == "SHIPPED" and again["cargo_key"] == f"{first}B"


async def test_a_refused_registration_leaves_the_order_to_ship(
    client: AsyncClient, admin_user: User, customer_user: User, catalog, fake_yurtici: FakeYurtici
):
    order = await _paid_order(client, admin_user, customer_user, catalog[0])
    fake_yurtici.fail = "Yurtiçi Kargo: İlçe bulunamadı"
    resp = await client.post(
        f"/api/v1/admin/orders/{order['id']}/ship", headers=auth_header(admin_user), json={"carrier": "YURTICI", "use_api": True}
    )
    assert resp.status_code == 502 and "İlçe bulunamadı" in resp.json()["detail"]
    after = (await client.get(f"/api/v1/admin/orders/{order['id']}", headers=auth_header(admin_user))).json()
    assert (after["status"], after["cargo_key"]) == ("APPROVED", None)


async def test_shipment_endpoints_are_for_admins_and_web_shipments_only(
    client: AsyncClient, admin_user: User, customer_user: User, catalog, fake_yurtici: FakeYurtici
):
    order = await _paid_order(client, admin_user, customer_user, catalog[0])
    url = f"/api/v1/admin/orders/{order['id']}/shipment"
    assert (await client.post(f"{url}/refresh", headers=auth_header(customer_user))).status_code == 403
    # Not shipped through the web service: nothing to ask Yurtiçi about.
    assert (await client.post(f"{url}/refresh", headers=auth_header(admin_user))).status_code == 409
    await client.post(
        f"/api/v1/admin/orders/{order['id']}/ship", headers=auth_header(admin_user), json={"tracking_code": "1"}
    )
    assert (await client.post(f"{url}/cancel", headers=auth_header(admin_user))).status_code == 409


async def test_a_karsi_odemeli_order_ships_on_the_alici_odemeli_account(
    client: AsyncClient, admin_user: User, customer_user: User, catalog, fake_yurtici: FakeYurtici, db_session: AsyncSession
):
    order = await _paid_order(client, admin_user, customer_user, catalog[0])
    row = await db_session.get(Order, uuid.UUID(order["id"]))
    row.shipping_payer = ShippingPayer.RECEIVER
    await db_session.commit()

    h = auth_header(admin_user)
    resp = await client.post(f"/api/v1/admin/orders/{order['id']}/ship", headers=h, json={"carrier": "YURTICI", "use_api": True})
    assert resp.status_code == 200, resp.text
    assert (await client.post(f"/api/v1/admin/orders/{order['id']}/shipment/refresh", headers=h)).status_code == 200
    assert (await client.post(f"/api/v1/admin/orders/{order['id']}/shipment/cancel", headers=h)).status_code == 200
    # Registered, refreshed and withdrawn on the AO account only.
    assert fake_yurtici.calls == [
        ("create", ShippingPayer.RECEIVER), ("query", ShippingPayer.RECEIVER), ("cancel", ShippingPayer.RECEIVER),
    ]

    # The sync asks the AO account about it too (other tests' shipments are the GO account's).
    again = await client.post(f"/api/v1/admin/orders/{order['id']}/ship", headers=h, json={"carrier": "YURTICI", "use_api": True})
    fake_yurtici.calls.clear()
    await sync_shipments(db_session)
    assert ("query", ShippingPayer.RECEIVER) in fake_yurtici.calls
    synced = await client.get(f"/api/v1/admin/orders/{order['id']}", headers=h)
    assert synced.json()["carrier_synced_at"] != again.json()["carrier_synced_at"]
