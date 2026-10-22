"""The arithmetic of the pricing worksheet, and writing its result to a product.

Two rows, as on the paper sheet. The calculated row is one unbroken formula
chain — every column is the previous *calculated* value plus its percent, in
the cost's own VAT basis, so 1000 at +15%, +15%, +20% is 1150, 1322.50, 1587
whatever is typed below it. The "uygulanacak fiyat" row is the admin's own
figure per column; it never feeds the chain, it only replaces that column's
calculated value as the price sent to the site.

Retail is the last column and, as on the paper sheet ("KDV dahil perakende
fiyat %KDV"), it is always after VAT: the previous calculated value plus its
percent, plus VAT when the sheet's cost was before VAT. Its "uygulanacak fiyat"
is the shelf price, after VAT too. The VAT rate is also what every price is
stored with, so the other side (before ↔ after VAT) can be completed.

Between the classes and retail may come "extras" ("Özel Fiyat"): parallel
branches, each worked out exactly as retail is — the last class plus its
percent, plus VAT on a before-VAT sheet. Neither builds on the other, so an
extra never moves retail. They are for the admin's reference only: no
customer group has such a price, so applying a sheet reports them as not
written.

"ERP Fiyatı (%)", when given, puts beside every calculated price ("erp") that
price plus the percent: a reference figure for the ERP, never sent anywhere.

The panel does the same sums live; the server does them again on save, so a
stored row never disagrees with its own figures.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.catalog import CurrencyType, Product, ProductVariant, VariantPrice
from app.models.user import CustomerSegment, SegmentType
from app.schemas.pricing import AppliedPrice, PricingSheetIn

CENT = Decimal("0.01")


def _step(previous: Decimal, percent: Decimal) -> Decimal:
    return previous * (1 + percent / 100)


def compute_sheet(sheet: PricingSheetIn) -> dict:
    """The worksheet with every column's calculated and final price, as stored."""

    def erp(computed: Decimal) -> dict:
        # Half a kuruş up, as the panel shows it (1322.50 + 5% = 1388.63).
        if sheet.erp_percent is None:
            return {}
        return {"erp": str(_step(computed, sheet.erp_percent).quantize(CENT, rounding=ROUND_HALF_UP))}

    previous = sheet.cost
    columns = []
    for col in sheet.columns:
        computed = _step(previous, col.percent)
        price = col.desired if col.desired is not None else computed.quantize(CENT)
        columns.append({
            "label": col.label,
            "segment": col.segment.value if col.segment else None,
            "percent": str(col.percent),
            "computed": str(computed.quantize(CENT)),
            **erp(computed),
            "desired": str(col.desired) if col.desired is not None else None,
            "price": str(price),
        })
        previous = computed  # the chain runs on calculated values only

    r = sheet.retail
    with_vat = (lambda v: v * (1 + r.vat_rate / 100)) if sheet.vat_mode == "EXCL" else (lambda v: v)
    extras = []
    for extra in sheet.extras:
        # A branch off the last class, as retail is: `previous` stays put.
        extra_before = _step(previous, extra.percent)
        extra_computed = with_vat(extra_before)
        extras.append({
            "label": extra.label,
            "segment": None,
            "percent": str(extra.percent),
            "vat_rate": str(r.vat_rate),
            "computed": str(extra_computed.quantize(CENT)),
            **erp(extra_computed),
            "before_vat": str(extra_before.quantize(CENT)),
            "desired": str(extra.desired) if extra.desired is not None else None,
            "price": str(extra.desired if extra.desired is not None else extra_computed.quantize(CENT)),
        })

    before_vat = _step(previous, r.percent)
    retail_computed = with_vat(before_vat)
    retail_price = r.desired if r.desired is not None else retail_computed.quantize(CENT)
    rate = {CurrencyType.USD: sheet.usd_rate, CurrencyType.EUR: sheet.eur_rate}.get(sheet.currency)
    try_price = (retail_price * rate).quantize(CENT) if rate else None
    return {
        "vat_mode": sheet.vat_mode,
        "currency": sheet.currency.value,
        "cost": str(sheet.cost),
        "columns": columns,
        "retail": {
            "label": "Perakende Fiyat",
            "segment": SegmentType.RETAIL.value,
            "percent": str(r.percent),
            "vat_rate": str(r.vat_rate),
            # After VAT; `before_vat` is the markup step on its own.
            "computed": str(retail_computed.quantize(CENT)),
            **erp(retail_computed),
            "before_vat": str(before_vat.quantize(CENT)),
            "desired": str(r.desired) if r.desired is not None else None,
            "price": str(retail_price),
        },
        "extras": extras,
        "erp_percent": str(sheet.erp_percent) if sheet.erp_percent is not None else None,
        "usd_rate": str(sheet.usd_rate) if sheet.usd_rate is not None else None,
        "eur_rate": str(sheet.eur_rate) if sheet.eur_rate is not None else None,
        "try_price": str(try_price) if try_price is not None else None,
        "final_try": str(sheet.final_try) if sheet.final_try is not None else None,
    }


class ApplyError(ValueError):
    pass


@dataclass
class Applied:
    product: Product
    variants: int
    prices: list[AppliedPrice]
    skipped: list[str]


def _both_sides(price: Decimal, vat_mode: str, vat_rate: Decimal) -> tuple[Decimal, Decimal]:
    """(excl, incl) from a price in the sheet's VAT basis."""
    factor = 1 + vat_rate / 100
    if vat_mode == "INCL":
        return (price / factor).quantize(CENT), price.quantize(CENT)
    return price.quantize(CENT), (price * factor).quantize(CENT)


async def find_product(db: AsyncSession, code: str) -> Product | None:
    """By the product's code, else by one of its colours' SKUs (case-insensitive)."""
    code = code.strip()
    if not code:
        return None
    options = selectinload(Product.variants)
    product = await db.scalar(select(Product).where(func.lower(Product.base_code) == code.lower()).options(options))
    if product is None:
        variant = await db.scalar(select(ProductVariant).where(func.lower(ProductVariant.sku) == code.lower()))
        if variant is not None:
            product = await db.scalar(select(Product).where(Product.id == variant.product_id).options(options))
    return product


async def apply_to_product(db: AsyncSession, sheet: PricingSheetIn, computed: dict) -> Applied:
    """Write the sheet's prices to every active colour of the product it names.

    One price per customer group, in the sheet's currency — except retail on a
    USD/EUR sheet with a "son karar", which is that TRY figure. A group's rows
    in the other currencies are removed: checkout charges a TRY row whenever
    there is one, so an old one would quietly win over the new price.
    """
    product = await find_product(db, sheet.product_code)
    if product is None:
        raise ApplyError(f"“{sheet.product_code}” kodlu ürün bulunamadı")
    variants = [v for v in product.variants if v.is_active]
    if not variants:
        raise ApplyError("Ürünün satıştaki rengi yok")

    segments = await segment_ids(db)
    vat_rate = sheet.retail.vat_rate
    targets: list[tuple[str, SegmentType, CurrencyType, Decimal, str]] = []
    skipped: list[str] = []
    for col in computed["columns"]:
        if col["segment"] is None:
            skipped.append(col["label"])
        else:
            targets.append((col["label"], SegmentType(col["segment"]), sheet.currency, Decimal(col["price"]), sheet.vat_mode))
    skipped += [extra["label"] for extra in computed["extras"]]
    retail = computed["retail"]
    if sheet.currency != CurrencyType.TRY and sheet.final_try is not None:
        # "Son karar" is a retail price after VAT, in TRY.
        targets.append((retail["label"], SegmentType.RETAIL, CurrencyType.TRY, sheet.final_try, "INCL"))
    else:
        # Retail is after VAT whatever the cost basis (see compute_sheet).
        targets.append((retail["label"], SegmentType.RETAIL, sheet.currency, Decimal(retail["price"]), "INCL"))

    applied: list[AppliedPrice] = []
    rows: list[tuple[int, CurrencyType, Decimal, Decimal]] = []
    for label, segment, currency, price, basis in targets:
        segment_id = segments.get(segment)
        if segment_id is None:
            raise ApplyError(f"Müşteri grubu tanımlı değil: {segment.value}")
        excl, incl = _both_sides(price, basis, vat_rate)
        rows.append((segment_id, currency, excl, incl))
        applied.append(AppliedPrice(
            label=label, segment=segment, currency=currency, price_excl_tax=excl, price_incl_tax=incl,
        ))
    await write_prices(db, variants, rows, vat_rate)
    return Applied(product=product, variants=len(variants), prices=applied, skipped=skipped)


async def segment_ids(db: AsyncSession) -> dict[SegmentType, int]:
    return {
        SegmentType(code): seg_id
        for seg_id, code in (await db.execute(select(CustomerSegment.id, CustomerSegment.code))).all()
    }


async def write_prices(
    db: AsyncSession,
    variants: list[ProductVariant],
    prices: list[tuple[int, CurrencyType, Decimal, Decimal]],
    vat_rate: Decimal,
) -> None:
    """Give every one of these colours each (segment, currency, excl, incl)
    price: the group's row in that currency is updated or added, and its rows
    in the other currencies are removed — checkout charges a TRY row whenever
    there is one, so an old one would quietly win over the new price."""
    # Read fresh rather than through `variant.prices`: a collection loaded
    # earlier in the session would not show rows written since.
    rows_by_variant: dict = {v.id: [] for v in variants}
    for row in (
        await db.execute(
            select(VariantPrice)
            .where(VariantPrice.variant_id.in_(list(rows_by_variant)))
            .execution_options(populate_existing=True)
        )
    ).scalars():
        rows_by_variant[row.variant_id].append(row)

    for segment_id, currency, excl, incl in prices:
        for variant in variants:
            kept = None
            for row in [p for p in rows_by_variant[variant.id] if p.segment_id == segment_id]:
                if CurrencyType(row.currency) == currency and kept is None:
                    kept = row
                else:
                    await db.delete(row)
            if kept is None:
                db.add(VariantPrice(
                    variant_id=variant.id, segment_id=segment_id, currency=currency,
                    price_excl_tax=excl, price_incl_tax=incl, vat_rate=vat_rate,
                ))
            else:
                kept.price_excl_tax, kept.price_incl_tax, kept.vat_rate = excl, incl, vat_rate
