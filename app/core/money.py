"""Money as the shop writes it to customers: "1.234,56 TL"."""

from decimal import Decimal


def format_tl(amount: Decimal | str | int | float) -> str:
    """Turkish grouping and decimal comma, the amount then "TL" — written out
    without a locale dependency, as the storefront shows it."""
    whole, _, cents = f"{Decimal(str(amount)):.2f}".partition(".")
    sign = "-" if whole.startswith("-") else ""
    grouped = f"{int(whole.lstrip('-')):,}".replace(",", ".")
    return f"{sign}{grouped},{cents} TL"
