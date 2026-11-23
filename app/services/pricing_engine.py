"""Price resolution: which row to charge, and what it is worth in TRY.

A segment's price may be entered in TRY, USD or EUR. TRY is charged as typed;
a foreign price is converted at the day's TCMB rate, because an order is always
booked in TRY (see app.services.exchange_rates).

A converted price is rounded up to whole lira (1163.86 → 1164.00): the shop
never shows or charges kuruş that only the exchange rate put there. The figure
before VAT is then derived from that whole price, so the two still agree.
"""

from collections.abc import Iterable
from decimal import ROUND_CEILING, Decimal


from app.models.catalog import CurrencyType, VariantPrice
from app.services.exchange_rates import Rates

# TRY first: if the admin typed a TRY price, that is the price. Otherwise the
# foreign row they did fill, tried in this order.
CURRENCY_PREFERENCE = (CurrencyType.TRY, CurrencyType.USD, CurrencyType.EUR)


def pick_price_row(prices: Iterable[VariantPrice], segment_id: int) -> VariantPrice | None:
    """The row that prices this variant for a segment, or None if it isn't sold to it."""
    rows = {CurrencyType(p.currency): p for p in prices if p.segment_id == segment_id}
    return next((rows[c] for c in CURRENCY_PREFERENCE if c in rows), None)


CENT = Decimal("0.01")


def whole_lira(amount: Decimal) -> Decimal:
    """`amount` rounded up to the next whole lira; a whole amount stays as it is
    (1163.86 → 1164.00, 1164.00 → 1164.00)."""
    return amount.to_integral_value(rounding=ROUND_CEILING).quantize(CENT)


def excl_from_incl(incl_tax: Decimal, vat_rate: Decimal) -> Decimal:
    """The price before VAT that `incl_tax` is, at `vat_rate` percent."""
    return (incl_tax / (1 + vat_rate / 100)).quantize(CENT)


def to_try(amount: Decimal, currency: CurrencyType | str, rates: Rates | None) -> Decimal | None:
    """`amount` in TRY, or None when the rate needed for it is unavailable.

    A TRY amount is returned as it is; a converted one is rounded up to whole
    lira (`whole_lira`)."""
    currency = CurrencyType(currency)
    if currency == CurrencyType.TRY:
        return amount
    rate = rates.try_per_unit.get(currency.value) if rates else None
    if not rate or rate <= 0:
        return None
    return whole_lira(amount * rate)


def row_in_try(row: VariantPrice, rates: Rates | None) -> tuple[Decimal, Decimal] | None:
    """(before VAT, after VAT) of a price row in TRY, or None without the rate.

    A foreign row is converted after VAT — the price that is charged — and its
    before-VAT figure follows from that whole-lira price, rather than being
    converted on its own and rounded apart from it."""
    if CurrencyType(row.currency) == CurrencyType.TRY:
        return row.price_excl_tax, row.price_incl_tax
    incl = to_try(row.price_incl_tax, row.currency, rates)
    if incl is None:
        return None
    return excl_from_incl(incl, row.vat_rate), incl


def calculate_line_total(unit_price: Decimal, quantity: int) -> Decimal:
    return (unit_price * quantity).quantize(Decimal("0.01"))
