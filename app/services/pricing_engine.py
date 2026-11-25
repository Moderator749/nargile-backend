"""Price resolution: which row to charge, and what it is worth in TRY.

A segment's price may be entered in TRY, USD or EUR. TRY is charged as typed;
a foreign price is converted at the day's TCMB rate, because an order is always
booked in TRY (see app.services.exchange_rates).

A converted RETAIL (Perakende) price is rounded up to whole lira
(1163.86 → 1164.00): a shopper never sees or pays kuruş that only the exchange
rate put there. The figure before VAT is then derived from that whole price, so
the two still agree. A business group's price (Bayi, Toptan, Kafe) keeps its
kuruş: it is converted to the kuruş, before and after VAT each.
"""

from collections.abc import Iterable
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal


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


def try_price(amount: Decimal, *, retail: bool) -> Decimal:
    """A TRY price after VAT as the shop keeps it: whole lira, rounded up, for
    retail (3.16 → 4.00); to the kuruş for a business group (3.164 → 3.16)."""
    return whole_lira(amount) if retail else amount.quantize(CENT, rounding=ROUND_HALF_UP)


def to_try(
    amount: Decimal, currency: CurrencyType | str, rates: Rates | None, *, retail: bool
) -> Decimal | None:
    """`amount` in TRY, or None when the rate needed for it is unavailable.

    A TRY amount is returned as it is; a converted one is rounded up to whole
    lira for retail (`whole_lira`), to the kuruş for a business group."""
    currency = CurrencyType(currency)
    if currency == CurrencyType.TRY:
        return amount
    rate = rates.try_per_unit.get(currency.value) if rates else None
    if not rate or rate <= 0:
        return None
    return try_price(amount * rate, retail=retail)


def row_in_try(row: VariantPrice, rates: Rates | None, *, retail: bool) -> tuple[Decimal, Decimal] | None:
    """(before VAT, after VAT) of a price row in TRY, or None without the rate.

    Retail: a foreign row is converted after VAT — the price that is charged —
    and its before-VAT figure follows from that whole-lira price. A business
    group: both figures are converted to the kuruş, each as it was entered."""
    if CurrencyType(row.currency) == CurrencyType.TRY:
        return row.price_excl_tax, row.price_incl_tax
    incl = to_try(row.price_incl_tax, row.currency, rates, retail=retail)
    if incl is None:
        return None
    if retail:
        return excl_from_incl(incl, row.vat_rate), incl
    return to_try(row.price_excl_tax, row.currency, rates, retail=False), incl


# An ERP price ("ERP ve Katalog Fiyatları") is kept before VAT, to the kuruş —
# or to four places when the kuruş would not give back the price after VAT it
# was typed as: 0.45 USD at 20% is 0.375, since 0.38 × 1.20 = 0.456 shows 0.46.
ERP_PLACES = Decimal("0.0001")


def erp_before_vat(price: Decimal, vat_rate: Decimal) -> Decimal:
    """An ERP price typed after VAT (`price`), before VAT as the product keeps
    it: to the kuruş when VAT put back on that gives `price` again (115 at 20%
    is 95.83, and 95.83 × 1.20 = 115.00), else to four places (0.45 is 0.375).
    At 0% it is `price` itself."""
    factor = 1 + Decimal(vat_rate) / 100
    exact = price / factor
    cents = exact.quantize(CENT, rounding=ROUND_HALF_UP)
    if (cents * factor).quantize(CENT, rounding=ROUND_HALF_UP) == price.quantize(CENT, rounding=ROUND_HALF_UP):
        return cents
    return exact.quantize(ERP_PLACES, rounding=ROUND_HALF_UP)


def erp_text(value: Decimal | str) -> str:
    """An ERP price as the product stores it: to the kuruş ("95.83", "105.00"),
    or with the places it needs beyond them, up to four ("0.375")."""
    exact = Decimal(value).quantize(ERP_PLACES, rounding=ROUND_HALF_UP)
    cents = exact.quantize(CENT)
    return str(cents) if cents == exact else str(exact).rstrip("0")


def calculate_line_total(unit_price: Decimal, quantity: int) -> Decimal:
    return (unit_price * quantity).quantize(Decimal("0.01"))
