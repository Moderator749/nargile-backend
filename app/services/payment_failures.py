"""Why a card payment failed, in terms the person on the phone can act on.

PayTR's failure notification carries a code (`failed_reason_code`) and a
message (`failed_reason_msg`). The code is coarse — "0" covers every refusal
the bank itself gives, and its words arrive in the message ("Yetersiz
bakiye", "Kartınız internet alışverişine kapalı") — so a specific code is
taken as it is, and a "0" (or a missing code) is read from its words.

Each failure lands on one side:

- CUSTOMER_BANK: the card, the cardholder or their bank said no. Nothing is
  wrong with the shop; the customer can try again, another way.
- SYSTEM_STORE: the shop's own set-up (its PayTR account, its integration,
  PayTR's fraud checks on its merchant account) stopped it. Someone at the shop
  has to look before the customer tries again.

The order's own failures — the payment page could not be opened, or was left
unpaid until it expired — have no PayTR notification and are read off the
order instead (see `for_order`).
"""

import enum
from dataclasses import dataclass
from datetime import datetime

from app.models.order import Order, OrderStatus
from app.models.payment import PaymentStatus, PaymentTransaction


class FailureSource(str, enum.Enum):
    CUSTOMER_BANK = "CUSTOMER_BANK"
    SYSTEM_STORE = "SYSTEM_STORE"


@dataclass(frozen=True)
class FailureVerdict:
    source: FailureSource
    # What the customer service rep should suggest, in Turkish, as the panel reads it.
    advice: str


@dataclass(frozen=True)
class PaymentFailure:
    source: FailureSource
    advice: str
    code: str | None
    message: str | None
    at: datetime | None
    merchant_oid: str | None
    test_mode: bool


_CB, _SS = FailureSource.CUSTOMER_BANK, FailureSource.SYSTEM_STORE

_LIMIT = FailureVerdict(
    _CB, "Müşteriye kart limitini veya bakiyesini kontrol etmesini ya da başka bir kartla denemesini önerin."
)
_ONLINE = FailureVerdict(
    _CB,
    "Müşteriye kartının internet alışverişine açık olduğunu bankasının mobil uygulamasından veya "
    "müşteri hizmetlerinden kontrol etmesini önerin.",
)
_3D = FailureVerdict(
    _CB,
    "3D Secure doğrulaması tamamlanamadı: müşteriye bankadan gelen SMS şifresini süresi dolmadan ve "
    "doğru girerek yeniden denemesini önerin.",
)
_CARD_DATA = FailureVerdict(
    _CB, "Kart bilgileri hatalı girilmiş: müşteriye kart numarası, son kullanma tarihi ve CVV'yi kontrol ederek yeniden denemesini önerin."
)
_CARD_BLOCKED = FailureVerdict(
    _CB, "Kart bankaca kullanıma kapalı: müşteriye bankasıyla görüşmesini veya başka bir kart kullanmasını önerin."
)
_INSTALLMENT = FailureVerdict(
    _CB, "Bu kart seçilen taksiti desteklemiyor: müşteriye tek çekim ya da başka bir kartla denemesini önerin."
)
_ABANDONED = FailureVerdict(
    _CB,
    "Müşteri ödeme sayfasını tamamlamadan ayrıldı: sepeti duruyor; arayıp siparişi yeniden vermesini "
    "veya havale ile ödemesini hatırlatabilirsiniz.",
)
_BANK = FailureVerdict(
    _CB, "Banka işlemi onaylamadı: müşteriye bankasıyla görüşmesini veya başka bir kartla denemesini önerin."
)
_FRAUD = FailureVerdict(
    _SS,
    "PayTR güvenlik kontrolü işlemi durdurdu: PayTR mağaza panelinden işlemi inceleyin, gerekirse "
    "müşteriyi arayıp doğrulayın; tekrar denemeden önce havale önerilebilir.",
)
_NON_3D = FailureVerdict(
    _SS, "Mağaza hesabı bu kart için 3D Secure'suz işleme kapalı: PayTR hesap ayarlarını kontrol edin."
)
_TECHNICAL = FailureVerdict(
    _SS,
    "Teknik / entegrasyon hatası: müşterinin yapabileceği bir şey yok. PayTR mağaza panelini ve sunucu "
    "kayıtlarını kontrol edin; sorun sürerse PayTR destek ile görüşün.",
)
_PAGE_REFUSED = FailureVerdict(
    _SS,
    "Ödeme sayfası açılamadı (PayTR token isteğini reddetti): PayTR mağaza bilgilerini ve hesap "
    "durumunu kontrol edin. Müşteriye bu arada havale önerilebilir.",
)

# PayTR's documented `failed_reason_code`s. "0", the bank's own refusal, is not
# here: its reason is only in the message (`_BY_WORDS`, then `_BANK`).
_BY_CODE: dict[str, FailureVerdict] = {
    "1": _3D,  # Kimlik doğrulama yapılmadı
    "2": _3D,  # Kimlik doğrulama başarısız
    "3": _FRAUD,  # Güvenlik kontrolü sonrası onay verilmedi
    "6": _ABANDONED,  # Müşteri ödeme sayfasından ayrıldı
    "8": _INSTALLMENT,  # Bu karta taksit yapılamamaktadır
    "9": _CARD_BLOCKED,  # Bu kart ile işlem yetkisi bulunmamaktadır
    "10": _NON_3D,  # Bu işlemde 3D Secure kullanılmalıdır
    "11": _FRAUD,  # Güvenlik uyarısı
    "99": _TECHNICAL,  # Teknik entegrasyon hatası
}

# Words in the message (folded to lowercase ASCII), in order: the first hit wins.
# "Limit" before "kart": "kart limiti yetersiz" is about the limit.
_BY_WORDS: tuple[tuple[tuple[str, ...], FailureVerdict], ...] = (
    (("bakiye", "limit", "yetersiz", "insufficient"), _LIMIT),
    (("internet", "e-ticaret", "eticaret", "sanal pos", "online", "e-commerce"), _ONLINE),
    (("3d", "dogrulama", "sms", "sifre", "zaman asimi", "timeout", "authentication"), _3D),
    (("son kullanma", "cvv", "cvc", "kart numarasi", "gecersiz kart", "invalid card", "expired"), _CARD_DATA),
    (("kayip", "calinti", "bloke", "kapali", "yetki", "stolen", "lost"), _CARD_BLOCKED),
    (("taksit", "installment"), _INSTALLMENT),
    (("vazgec", "ayrildi", "tamamlanmadi"), _ABANDONED),
    (("guvenlik", "fraud", "risk"), _FRAUD),
    (("entegrasyon", "teknik", "hash", "merchant", "magaza", "token", "sayfasi acilamadi"), _TECHNICAL),
)

_TR_FOLD = str.maketrans("İIıÖöÜüŞşÇçĞğÂâÎîÛû", "iiioouussccggaaiiuu")


def _fold(text: str) -> str:
    return text.translate(_TR_FOLD).lower()


def classify(code: str | None, message: str | None) -> FailureVerdict:
    """Which side a failure is on, and what to tell the customer.

    A specific code is PayTR's own verdict and stands ("10" mentions 3D Secure
    but is the shop's set-up, not the cardholder's). Only the bank's catch-all
    "0" — or no code at all — is read from its words.
    """
    code = (code or "").strip() or None
    if code in _BY_CODE:
        return _BY_CODE[code]
    words = _fold(message or "")
    for keys, verdict in _BY_WORDS:
        if any(k in words for k in keys):
            return verdict
    if code is None or code == "0":
        return _BANK
    # A code PayTR has not documented, with words we do not know: the shop's to look into.
    return _TECHNICAL


def for_order(order: Order, transactions: list[PaymentTransaction]) -> PaymentFailure | None:
    """The failure behind a PAYMENT_FAILED order, or None for any other order.

    The latest failed attempt PayTR reported, when there is one. Otherwise the
    order failed on its own: the payment page was refused, or left unpaid
    until the hourly job released it (`cancel_reason` says which).
    """
    if order.status != OrderStatus.PAYMENT_FAILED:
        return None
    failed = [t for t in transactions if t.status == PaymentStatus.FAILED]
    if failed:
        tx = max(failed, key=lambda t: t.completed_at or t.created_at)
        verdict = classify(tx.failed_reason_code, tx.failed_reason_msg)
        return PaymentFailure(
            source=verdict.source, advice=verdict.advice,
            code=tx.failed_reason_code, message=tx.failed_reason_msg or order.cancel_reason,
            at=tx.completed_at or order.cancelled_at, merchant_oid=tx.merchant_oid, test_mode=tx.test_mode,
        )
    reason = order.cancel_reason or ""
    if "sayfası açılamadı" in reason:
        verdict = _PAGE_REFUSED
    elif "tamamlanmadı" in reason:
        verdict = _ABANDONED
    else:
        verdict = classify(None, reason)
    latest = max(transactions, key=lambda t: t.created_at, default=None)
    return PaymentFailure(
        source=verdict.source, advice=verdict.advice, code=None, message=reason or None,
        at=order.cancelled_at, merchant_oid=latest.merchant_oid if latest else None,
        test_mode=latest.test_mode if latest else False,
    )
