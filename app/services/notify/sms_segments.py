"""How many SMS a text costs: GSM 03.38 7-bit, or UCS-2 when any character
falls outside it (ğ, ı, ş, İ do — Turkish text often goes UCS-2).

Mirrored by saas_frontend/src/lib/sms.ts for the live counter.
"""

GSM_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM_EXTENDED = set("^{}\\[~]|€\f")


def sms_parts(text: str) -> tuple[str, int, int]:
    """(encoding, characters as billed, number of SMS)."""
    if all(c in GSM_BASIC or c in GSM_EXTENDED for c in text):
        units = sum(2 if c in GSM_EXTENDED else 1 for c in text)
        single, multi, encoding = 160, 153, "GSM-7"
    else:
        # UTF-16 code units: an emoji takes two.
        units = len(text.encode("utf-16-le")) // 2
        single, multi, encoding = 70, 67, "UCS-2"
    if units == 0:
        return encoding, 0, 0
    return encoding, units, 1 if units <= single else -(-units // multi)
