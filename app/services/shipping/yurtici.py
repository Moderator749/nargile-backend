"""Yurtiçi Kargo's web service (KOPS "ShippingOrderDispatcherServices", SOAP).

The flow, as the service works it:

1. ``createShipment`` registers the parcel under our cargo key — a reference of
   our own, printed as a barcode on the label. Nothing is collected yet.
2. The branch scans that barcode when the parcel is handed over. From then on
   ``queryShipment`` reports Yurtiçi's own document number for it (the
   "gönderi kodu" a customer tracks by) and its tracking page.
3. ``cancelShipment`` withdraws a registration whose parcel has not been
   collected ("Kargo çıkışı engellendi").

Status codes it reports (``operationStatus``): NOP not yet processed, IND in
delivery, ISR processed, DLV delivered, CNL cancelled.

Endpoints, from Yurtiçi's own WSDLs (both advertise plain HTTP, which does not
answer; HTTPS does). Test credentials are Yurtiçi's public pair, YKTEST / YK.
Requests are built and responses read here; nothing touches the database.

Each parcel also carries our reference in the "Sipariş No" special field
(``specialField1``, "3$<ref>#"), which is what Yurtiçi's self-service report
finds it by: ``reference_tracking_url`` links to that report from the moment
the shipment is registered, before the branch has scanned anything.
"""

import xml.etree.ElementTree as ET
from datetime import date
from urllib.parse import urlencode, urlsplit, urlunsplit
from xml.sax.saxutils import escape

import httpx

from app.services.shipping.base import ShipmentRequest, ShipmentStatus, ShippingError

LIVE_URL = "https://ws.yurticikargo.com/KOPSWebServices/ShippingOrderDispatcherServices"
TEST_URL = "https://testws.yurticikargo.com/KOPSWebServices/ShippingOrderDispatcherServices"
NAMESPACE = "http://yurticikargo.com.tr/ShippingOrderDispatcherServices"

#: Where a customer follows a Yurtiçi shipment by its gönderi kodu.
TRACKING_URL = "https://www.yurticikargo.com/Tracking/Detail/{code}"
#: Yurtiçi's self-service report: a shipment by the value of one of its special fields.
SELF_SERVICE_URL = "http://selfservis.yurticikargo.com/reports/SswReportsFromParamFields.aspx"
#: The special field that holds the order number ("Sipariş No").
ORDER_NO_FIELD = 3

DELIVERED = "DLV"
CANCELLED = "CNL"


def tracking_url(code: str) -> str:
    return TRACKING_URL.format(code=code.strip())


def reference_tracking_url(customer_code: str, reference: str, day: date) -> str:
    """The self-service report for the shipment carrying ``reference`` as its
    Sipariş No, registered by ``customer_code`` (müşteri kodu) on ``day``."""
    query = urlencode(
        {"ssfldvn": ORDER_NO_FIELD, "sskurkod": customer_code, "refnumber": reference, "date": day.strftime("%d.%m.%Y")}
    )
    return f"{SELF_SERVICE_URL}?{query}"


def is_reference_url(url: str | None) -> bool:
    return bool(url) and url.startswith(SELF_SERVICE_URL)


def service_url(wsdl_url: str) -> str:
    """Where the operations are posted: the WSDL's address without its query."""
    parts = urlsplit(wsdl_url.strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _el(name: str, value: object | None) -> str:
    if value is None or value == "":
        return ""
    return f"<{name}>{escape(str(value))}</{name}>"


def envelope(operation: str, body: str) -> bytes:
    return (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:ship="{NAMESPACE}"><soapenv:Header/><soapenv:Body>'
        f"<ship:{operation}>{body}</ship:{operation}>"
        "</soapenv:Body></soapenv:Envelope>"
    ).encode()


def create_body(user: str, password: str, language: str, s: ShipmentRequest) -> str:
    # The cargo key doubles as the invoice key: one parcel, one reference.
    order = "".join(
        (
            _el("cargoKey", s.cargo_key),
            _el("invoiceKey", s.cargo_key),
            _el("receiverCustName", s.receiver_name[:200]),
            _el("receiverAddress", s.address[:500]),
            _el("cityName", s.city),
            _el("townName", s.district),
            _el("receiverPhone1", s.phone),
            _el("emailAddress", s.email),
            _el("cargoCount", s.pieces),
            _el("specialField1", f"{ORDER_NO_FIELD}${s.cargo_key}#"),
            _el("description", (s.note or "")[:200]),
        )
    )
    return (
        _el("wsUserName", user) + _el("wsPassword", password) + _el("userLanguage", language)
        + f"<ShippingOrderVO>{order}</ShippingOrderVO>"
    )


def query_body(user: str, password: str, language: str, cargo_keys: list[str]) -> str:
    return (
        _el("wsUserName", user) + _el("wsPassword", password) + _el("wsLanguage", language)
        + "".join(_el("keys", key) for key in cargo_keys)
        # keyType 0: the keys are our cargo keys. Not just tracking, the whole record.
        + "<keyType>0</keyType><addHistoricalData>false</addHistoricalData><onlyTracking>false</onlyTracking>"
    )


def cancel_body(user: str, password: str, language: str, cargo_key: str) -> str:
    return (
        _el("wsUserName", user) + _el("wsPassword", password) + _el("userLanguage", language)
        + _el("cargoKeys", cargo_key)
    )


# ── Reading the answers (namespace-blind: the payload's own elements are unqualified) ──


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(node: ET.Element, name: str) -> ET.Element | None:
    return next((e for e in node.iter() if _local(e.tag) == name), None)


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in node if _local(e.tag) == name]


def _text(node: ET.Element | None, name: str) -> str | None:
    if node is None:
        return None
    child = next((e for e in node if _local(e.tag) == name), None)
    value = (child.text or "").strip() if child is not None else ""
    return value or None


def _result(xml: bytes | str, container: str) -> ET.Element:
    """The response's result element, or ShippingError with Yurtiçi's reason."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ShippingError(f"Yurtiçi Kargo'dan anlaşılmayan yanıt: {exc}") from exc
    fault = _find(root, "Fault")
    if fault is not None:
        raise ShippingError(f"Yurtiçi Kargo: {_text(fault, 'faultstring') or 'SOAP hatası'}")
    result = _find(root, container)
    if result is None:
        raise ShippingError("Yurtiçi Kargo'dan beklenmeyen yanıt")
    if _text(result, "outFlag") not in (None, "0"):
        raise ShippingError(f"Yurtiçi Kargo: {_text(result, 'outResult') or 'İstek reddedildi'}")
    return result


def parse_create(xml: bytes | str, cargo_key: str) -> None:
    result = _result(xml, "ShippingOrderResultVO")
    for detail in _children(result, "shippingOrderDetailVO"):
        if _text(detail, "cargoKey") == cargo_key and _text(detail, "errCode"):
            raise ShippingError(f"Yurtiçi Kargo: {_text(detail, 'errMessage') or 'Gönderi oluşturulamadı'}")


def _status(detail: ET.Element) -> ShipmentStatus:
    key = _text(detail, "cargoKey") or ""
    if _text(detail, "errCode"):
        message = _text(detail, "errMessage") or "Gönderi bulunamadı"
        return ShipmentStatus(cargo_key=key, code="ERR", text=message, error=message)
    code = _text(detail, "operationStatus") or ""
    item = next(iter(_children(detail, "shippingDeliveryItemDetailVO")), None)
    number = _text(item, "docId")
    if number == "0":
        number = None
    event = _text(item, "cargoEventExplanation")
    return ShipmentStatus(
        cargo_key=key,
        code=code,
        text=event or _text(detail, "operationMessage") or code,
        tracking_number=number,
        tracking_url=_text(item, "trackingUrl") or (tracking_url(number) if number else None),
        delivered=code == DELIVERED,
        cancelled=code == CANCELLED,
    )


def parse_query(xml: bytes | str) -> list[ShipmentStatus]:
    result = _result(xml, "ShippingDeliveryVO")
    return [_status(detail) for detail in _children(result, "shippingDeliveryDetailVO")]


def parse_cancel(xml: bytes | str, cargo_key: str) -> ShipmentStatus:
    result = _result(xml, "ShippingOrderResultVO")
    for detail in _children(result, "shippingCancelDetailVO"):
        if _text(detail, "cargoKey") == cargo_key:
            if _text(detail, "errCode"):
                raise ShippingError(f"Yurtiçi Kargo: {_text(detail, 'errMessage') or 'İptal edilemedi'}")
            return _status(detail)
    raise ShippingError("Yurtiçi Kargo iptali onaylamadı")


class YurticiProvider:
    def __init__(self, user: str, password: str, *, url: str = TEST_URL, language: str = "TR"):
        self.user, self.password, self.language, self.url = user, password, language, url

    async def _post(self, operation: str, body: str) -> bytes:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    self.url,
                    content=envelope(operation, body),
                    headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""},
                )
        except httpx.HTTPError as exc:
            raise ShippingError(f"Yurtiçi Kargo'ya ulaşılamadı: {exc}") from exc
        # A SOAP fault comes back as a 500 with a body worth reading.
        if resp.status_code >= 400 and b"Fault" not in resp.content:
            raise ShippingError(f"Yurtiçi Kargo yanıt vermedi (HTTP {resp.status_code})")
        return resp.content

    async def create(self, shipment: ShipmentRequest) -> None:
        xml = await self._post("createShipment", create_body(self.user, self.password, self.language, shipment))
        parse_create(xml, shipment.cargo_key)

    async def query(self, cargo_keys: list[str]) -> list[ShipmentStatus]:
        if not cargo_keys:
            return []
        return parse_query(await self._post("queryShipment", query_body(self.user, self.password, self.language, cargo_keys)))

    async def cancel(self, cargo_key: str) -> ShipmentStatus:
        xml = await self._post("cancelShipment", cancel_body(self.user, self.password, self.language, cargo_key))
        return parse_cancel(xml, cargo_key)
