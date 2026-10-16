"""Turkish mobile numbers as gateways want them: 905XXXXXXXXX."""

import re


def normalize_tr_mobile(raw: str | None) -> str | None:
    """'0532 123 45 67', '+90 (532) 123-45-67', '5321234567' → '905321234567';
    None for anything that is not a Turkish mobile (landlines, abroad)."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("90") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    return f"90{digits}" if len(digits) == 10 and digits.startswith("5") else None
