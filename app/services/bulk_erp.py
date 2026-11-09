""""Fiyat Giriş Modu": TL ERP prices typed in the admin's products list, and
the site prices that follow from them, as the pricing worksheet would set them.

A product's rule is the worksheet row last applied to it (by its code, else a
colour's stock code, as the worksheet finds products). For a group the
worksheet has a column for, the site price is the ERP price plus the sheet's
"uygulanacak oran", in the sheet's own VAT basis, half a kuruş up — the
worksheet's own `_priced`; retail and the extras never take the rate, so
retail's site price is its ERP price. A group the worksheet does not price, or
a product with no worksheet, goes on the site at its ERP price.

The ERP price is typed before VAT, as "ERP ve Katalog Fiyatları" keeps it, and
lands there in TRY. The site price is written in TRY to every colour on sale,
removing the group's rows in other currencies (`write_prices`). A TRY worksheet
row is brought up to date too — the new ERP price in its column, a typed
"uygulanacak fiyat" there cleared, its figures and its "applied" summary
recomputed — so applying it again later sets the same prices, not the old
ones. A USD/EUR worksheet cannot hold a TL price: it is left as it is, and
only lends its rate.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.catalog import CurrencyType, Product, ProductVariant, VariantPrice
from app.models.pricing import PricingRecord
from app.models.user import SegmentType
from app.schemas.admin import ErpPriceApplied, ErpPriceChange, PriceRule
from app.schemas.pricing import PricingSheetIn
from app.services.pricing_sheet import _both_sides, _step, compute_sheet, segment_ids, write_prices

CENT = Decimal("0.01")
# The catalogue's order: the three B2B lists, then retail.
GROUP_ORDER = (SegmentType.DEALER, SegmentType.WHOLESALE, SegmentType.CAFE, SegmentType.RETAIL)
# A product with no worksheet and no price row to take the rate from.
DEFAULT_VAT = Decimal("20")


class BulkErpError(ValueError):
    pass


async def governing_records(db: AsyncSession, products: list[Product]) -> dict[uuid.UUID, PricingRecord]:
    """Each product's worksheet row last applied, matched as `find_product`
    does: by the product's code, else by a colour's stock code. The products'
    variants must be loaded."""
    by_code: dict[str, uuid.UUID] = {p.base_code.strip().lower(): p.id for p in products}
    by_sku: dict[str, uuid.UUID] = {}
    for p in products:
        for v in p.variants:
            sku = (v.sku or "").strip().lower()
            if sku and sku not in by_code:
                by_sku.setdefault(sku, p.id)
    keys = {**by_sku, **by_code}
    if not keys:
        return {}
    records = await db.scalars(
        select(PricingRecord)
        .where(PricingRecord.applied_at.is_not(None), func.lower(func.trim(PricingRecord.product_code)).in_(list(keys)))
        .order_by(PricingRecord.applied_at.desc(), PricingRecord.id)
    )
    found: dict[uuid.UUID, PricingRecord] = {}
    for record in records:
        found.setdefault(keys[record.product_code.strip().lower()], record)
    return found


def _priced_groups(sheet: dict) -> set[SegmentType]:
    groups = {SegmentType(c["segment"]) for c in sheet.get("columns", []) if c.get("segment")}
    return groups | {SegmentType.RETAIL}


def _applied_percent(sheet: dict) -> Decimal | None:
    raw = sheet.get("applied_percent")
    return Decimal(raw) if raw not in (None, "") else None


def price_rule(record: PricingRecord) -> PriceRule:
    sheet = record.sheet
    return PriceRule(
        record_id=record.id,
        applied_at=record.applied_at,
        currency=CurrencyType(sheet.get("currency", "TRY")),
        applied_percent=_applied_percent(sheet),
        segments=[g for g in GROUP_ORDER if g in _priced_groups(sheet)],
    )


@dataclass
class SitePrice:
    excl: Decimal
    incl: Decimal
    vat_rate: Decimal
    applied_percent: Decimal | None
    from_worksheet: bool
    # The ERP price as the worksheet column holds it (its own VAT basis; retail after VAT).
    sheet_erp: Decimal | None = None


def site_price(erp: Decimal, segment: SegmentType, sheet: dict | None, fallback_vat: Decimal) -> SitePrice:
    """The site price for a group's ERP price (before VAT) under the worksheet, if any."""
    if sheet is None or segment not in _priced_groups(sheet):
        vat = Decimal(sheet["retail"]["vat_rate"]) if sheet else fallback_vat
        excl, incl = _both_sides(erp, "EXCL", vat)
        return SitePrice(excl, incl, vat, None, False)
    vat = Decimal(sheet["retail"]["vat_rate"])
    if segment is SegmentType.RETAIL:
        # Retail's ERP price is its price; the worksheet keeps it after VAT.
        excl, incl = _both_sides(erp, "EXCL", vat)
        return SitePrice(excl, incl, vat, None, True, sheet_erp=incl)
    mode = sheet.get("vat_mode", "EXCL")
    base = _both_sides(erp, "EXCL", vat)[1] if mode == "INCL" else erp.quantize(CENT)
    percent = _applied_percent(sheet)
    price = _step(base, percent).quantize(CENT, rounding=ROUND_HALF_UP) if percent else base
    excl, incl = _both_sides(price, mode, vat)
    return SitePrice(excl, incl, vat, percent, True, sheet_erp=base)


def sheet_input(record: PricingRecord) -> PricingSheetIn | None:
    """The worksheet row as the calculator would send it, from what it stored;
    None for a row stored in a shape the calculator no longer takes."""
    s = record.sheet
    try:
        return PricingSheetIn(
            product_code=record.product_code,
            product_name=record.product_name,
            vat_mode=s.get("vat_mode", "EXCL"),
            currency=s.get("currency", "TRY"),
            cost=s["cost"],
            columns=[
                {k: c.get(k) for k in ("label", "segment", "percent", "desired", "erp_price")} for c in s.get("columns", [])
            ],
            retail={k: s["retail"].get(k) for k in ("percent", "vat_rate", "desired", "erp_price")},
            extras=[{k: e.get(k) for k in ("label", "percent", "desired", "erp_price")} for e in s.get("extras", [])],
            applied_percent=s.get("applied_percent"),
            usd_rate=s.get("usd_rate"),
            eur_rate=s.get("eur_rate"),
            final_try=s.get("final_try"),
        )
    except (KeyError, TypeError, ValidationError):
        return None


def _update_record(record: PricingRecord, written: dict[SegmentType, SitePrice], now: datetime) -> None:
    """A TRY worksheet row given the new ERP prices (their typed "uygulanacak
    fiyat" cleared, as the ERP price now decides), recomputed, and its applied
    summary's prices for those groups replaced."""
    sheet = sheet_input(record)
    if sheet is None:
        return
    for segment, price in written.items():
        if segment is SegmentType.RETAIL:
            sheet.retail.erp_price, sheet.retail.desired = price.sheet_erp, None
            continue
        for column in sheet.columns:
            if column.segment is segment:
                column.erp_price, column.desired = price.sheet_erp, None
    record.sheet = compute_sheet(sheet)
    summary = dict(record.applied_summary or {})
    prices = [p for p in summary.get("prices", []) if SegmentType(p["segment"]) not in written]
    labels = {p["segment"]: p.get("label") for p in summary.get("prices", [])}
    for segment, price in written.items():
        prices.append({
            "label": labels.get(segment.value) or segment.value,
            "segment": segment.value,
            "currency": CurrencyType.TRY.value,
            "price_excl_tax": str(price.excl),
            "price_incl_tax": str(price.incl),
        })
    summary["prices"] = prices
    record.applied_summary = summary
    record.applied_at = record.updated_at = now


def _fallback_vat(product: Product, segment_id: int) -> Decimal:
    """The VAT rate the group's prices carry now, else any of the product's, else 20%."""
    rows: list[VariantPrice] = [p for v in product.variants if v.is_active for p in v.prices]
    for row in rows:
        if row.segment_id == segment_id:
            return row.vat_rate
    return rows[0].vat_rate if rows else DEFAULT_VAT


async def apply_erp_changes(db: AsyncSession, changes: list[ErpPriceChange]) -> list[ErpPriceApplied]:
    """Write the typed cells, all or none (the caller commits). Raises
    BulkErpError for a cell twice, or a product missing or deleted."""
    seen = set()
    for change in changes:
        key = (change.product_id, change.segment)
        if key in seen:
            raise BulkErpError("Bir hücre iki kez gönderildi")
        seen.add(key)

    ids = list(dict.fromkeys(c.product_id for c in changes))
    products = {
        p.id: p
        for p in (
            await db.scalars(
                select(Product)
                .where(Product.id.in_(ids), Product.is_deleted.is_(False))
                .options(selectinload(Product.variants).selectinload(ProductVariant.prices))
                .with_for_update(of=Product)
            )
        )
    }
    missing = [str(i) for i in ids if i not in products]
    if missing:
        raise BulkErpError(f"Ürün bulunamadı ya da silinmiş: {', '.join(missing[:5])}")

    segments = await segment_ids(db)
    records = await governing_records(db, list(products.values()))
    now = datetime.now(timezone.utc)
    applied: list[ErpPriceApplied] = []
    for product_id in ids:
        product = products[product_id]
        record = records.get(product_id)
        sheet = record.sheet if record is not None else None
        variants = [v for v in product.variants if v.is_active]
        matrix = {c: dict(row) for c, row in (product.erp_prices or {}).items()}
        tl = matrix.setdefault(CurrencyType.TRY.value, {})
        written: dict[SegmentType, SitePrice] = {}
        for change in (c for c in changes if c.product_id == product_id):
            if change.price is None:
                tl.pop(change.segment.value, None)
                applied.append(ErpPriceApplied(product_id=product_id, segment=change.segment, erp_price=None))
                continue
            tl[change.segment.value] = str(change.price.quantize(CENT))  # as the worksheet writes it
            segment_id = segments.get(change.segment)
            if segment_id is None:
                raise BulkErpError(f"Müşteri grubu tanımlı değil: {change.segment.value}")
            price = site_price(change.price, change.segment, sheet, _fallback_vat(product, segment_id))
            if variants:
                await write_prices(db, variants, [(segment_id, CurrencyType.TRY, price.excl, price.incl)], price.vat_rate)
                written[change.segment] = price
            applied.append(ErpPriceApplied(
                product_id=product_id,
                segment=change.segment,
                erp_price=change.price,
                site_price_excl=price.excl if variants else None,
                site_price_incl=price.incl if variants else None,
                applied_percent=price.applied_percent,
                from_worksheet=price.from_worksheet,
            ))
        product.erp_prices = {c: row for c, row in matrix.items() if row} or None
        # Only the groups the worksheet prices are its business.
        ours = {segment: price for segment, price in written.items() if price.from_worksheet}
        if record is not None and ours and sheet.get("currency", "TRY") == CurrencyType.TRY.value:
            _update_record(record, ours, now)
    await db.flush()
    return applied
