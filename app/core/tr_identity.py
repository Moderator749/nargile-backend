"""Turkish tax and identity numbers.

VKN (vergi kimlik numarası) is 10 digits and belongs to a firm. TCKN (T.C.
kimlik numarası) is 11 digits, belongs to a person, and carries a checksum —
which is worth verifying, because a mistyped identity number is only discovered
when an invoice is rejected.
"""

import re

_NON_DIGITS = re.compile(r"\D")


def digits_only(value: str) -> str:
    return _NON_DIGITS.sub("", value or "")


def is_valid_tckn(value: str) -> bool:
    """The official checksum: the 10th digit follows from the first nine, the
    11th from the first ten."""
    if not re.fullmatch(r"[1-9]\d{10}", value):
        return False
    d = [int(c) for c in value]
    odd = d[0] + d[2] + d[4] + d[6] + d[8]
    even = d[1] + d[3] + d[5] + d[7]
    if (odd * 7 - even) % 10 != d[9]:
        return False
    return sum(d[:10]) % 10 == d[10]


def normalize_tckn(value: str) -> str:
    """An 11-digit identity number, or ValueError."""
    found = digits_only(value)
    if not is_valid_tckn(found):
        raise ValueError("TC kimlik numarası 11 haneli ve geçerli olmalı")
    return found


def normalize_vkn(value: str) -> str:
    """A 10-digit tax number, or ValueError."""
    found = digits_only(value)
    if not re.fullmatch(r"\d{10}", found):
        raise ValueError("Vergi numarası 10 haneli olmalı")
    return found


def normalize_tax_id(value: str) -> str:
    """A sole proprietor is registered under either, so accept both: a 10-digit
    VKN, or an 11-digit TCKN that passes its checksum."""
    found = digits_only(value)
    if re.fullmatch(r"\d{10}", found):
        return found
    if len(found) == 11:
        return normalize_tckn(found)
    raise ValueError("Vergi numarası 10 (VKN) veya 11 (TC) haneli olmalı")
