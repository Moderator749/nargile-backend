"""MLZ163 (2026-11-24): stale ERP prices in another currency, and what deleted
products left behind, once.

    .venv/bin/python scripts/fix_erp_and_deleted_leftovers.py                     # dry run: what it would do
    .venv/bin/python scripts/fix_erp_and_deleted_leftovers.py --code MLZ163 --apply

1. ERP prices ("ERP ve Katalog Fiyatları") of a live product whose latest
   applied worksheet row was in one currency, made as applying it does now
   (pricing_sheet.write_erp):
   - a group that row priced loses its ERP prices in the other currencies
     (MLZ163: TL 15 / 15 / 20 / 45 beside the USD sheet's 0.15 / 0.15 / 0.20);
   - an ERP price the row wrote after VAT to the kuruş, where the kuruş does
     not give the typed price back, is kept exactly (retail 0.45 USD at 20%:
     0.38, shown 0.46, becomes 0.375).
   Only a group whose ERP price in the row's currency is still what that row
   wrote is touched: one edited on the product sheet since is the admin's.
   Every product is listed; only those named with --code are written (or
   all of them with --all-erp).
2. Products deleted before soft_delete did this itself: their applied
   worksheet rows are detached (no longer "Sitede", `product_id` null), their
   import aliases dropped, links to them as "birlikte satılanlar" removed and
   their flash sales switched off. Written with --apply.

Nothing is written on a dry run. A write saves every old figure first, to
scripts/migration_reports/fix-erp-and-deleted-leftovers-<time>.json.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError  # noqa: E402
from sqlalchemy import delete, select, update  # noqa: E402

from app.core.database import async_session, engine  # noqa: E402
from app.models.catalog import Product, ProductCrossSell, ProductVariant  # noqa: E402
from app.models.pricing import PriceImportAlias, PricingRecord  # noqa: E402
from app.models.promotion import FlashSale  # noqa: E402
from app.models.user import SegmentType  # noqa: E402
from app.schemas.pricing import PricingSheetIn  # noqa: E402
from app.services.pricing_engine import erp_text  # noqa: E402
from app.services.pricing_sheet import erp_by_group  # noqa: E402

REPORTS = Path(__file__).resolve().parent / "migration_reports"
CENT = Decimal("0.01")


def _old_erp(sheet: PricingSheetIn) -> dict[SegmentType, str | None]:
    """The ERP prices that row wrote before this fix: to the kuruş, half up."""
    vat = 1 + sheet.retail.vat_rate / 100

    def before(price: Decimal | None, factor: Decimal) -> str | None:
        return None if price is None else str((price / factor).quantize(CENT, rounding=ROUND_HALF_UP))

    classes = vat if sheet.vat_mode == "INCL" else Decimal(1)
    old = {c.segment: before(c.erp_price, classes) for c in sheet.columns if c.segment is not None}
    old[SegmentType.RETAIL] = before(sheet.retail.erp_price, vat)
    return old


def _fixed_erp(product: Product, sheet: PricingSheetIn) -> tuple[bool, dict | None]:
    """(changed, the product's ERP matrix made right for that row)."""
    matrix = {c: dict(row) for c, row in (product.erp_prices or {}).items()}
    mine = matrix.setdefault(sheet.currency.value, {})
    old, new = _old_erp(sheet), erp_by_group(sheet)
    for segment, written in old.items():
        current = mine.get(segment.value)
        target = erp_text(new[segment]) if new[segment] is not None else None
        if current not in (written, target):
            continue  # edited on the product sheet since: the admin's
        if target is not None:
            mine[segment.value] = target
        for currency, other in matrix.items():
            if currency != sheet.currency.value:
                other.pop(segment.value, None)
    fixed = {c: r for c, r in matrix.items() if r} or None
    return fixed != (product.erp_prices or None), fixed


async def _erp_changes(db) -> list[tuple[Product, PricingRecord, dict | None]]:
    """Each live product whose ERP prices its latest applied row would write otherwise now."""
    latest: dict[str, PricingRecord] = {}
    for record in (
        await db.scalars(
            select(PricingRecord).where(PricingRecord.applied_at.is_not(None)).order_by(PricingRecord.applied_at.desc())
        )
    ).all():
        pid = (record.applied_summary or {}).get("product_id")
        if pid:
            latest.setdefault(pid, record)
    products = (
        await db.scalars(select(Product).where(Product.is_deleted.is_(False), Product.erp_prices.is_not(None)))
    ).all()
    changes = []
    for product in products:
        record = latest.get(str(product.id))
        if record is None:
            continue
        try:
            sheet = PricingSheetIn.model_validate(
                {**record.sheet, "product_code": record.product_code, "product_name": record.product_name}
            )
        except ValidationError:
            continue
        changed, fixed = _fixed_erp(product, sheet)
        if changed:
            changes.append((product, record, fixed))
    return changes


async def run(codes: set[str], all_erp: bool, apply: bool) -> dict:
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        report: dict = {"at": now.isoformat(), "applied": apply, "erp": [], "deleted": {}}

        for product, record, fixed in await _erp_changes(db):
            chosen = all_erp or product.base_code.lower() in codes
            report["erp"].append({
                "product_id": str(product.id), "code": product.base_code, "record_id": str(record.id),
                "sheet_currency": record.sheet.get("currency"),
                "old": product.erp_prices, "new": fixed, "written": apply and chosen,
            })
            if apply and chosen:
                product.erp_prices = fixed

        deleted = select(Product.id).where(Product.is_deleted.is_(True))
        deleted_ids = {str(i) for i in (await db.scalars(deleted)).all()}
        records = [
            r for r in (await db.scalars(select(PricingRecord).where(PricingRecord.applied_at.is_not(None)))).all()
            if (r.applied_summary or {}).get("product_id") in deleted_ids
        ]
        aliases = (await db.scalars(select(PriceImportAlias).where(PriceImportAlias.product_id.in_(deleted)))).all()
        links = (
            await db.scalars(select(ProductCrossSell).where(ProductCrossSell.related_product_id.in_(deleted)))
        ).all()
        deleted_variants = select(ProductVariant.id).join(Product).where(Product.is_deleted.is_(True))
        sales = (
            await db.scalars(
                select(FlashSale).where(FlashSale.variant_id.in_(deleted_variants), FlashSale.is_active.is_(True))
            )
        ).all()
        report["deleted"] = {
            "records": [
                {"id": str(r.id), "code": r.product_code, "applied_at": r.applied_at.isoformat(), "summary": r.applied_summary}
                for r in records
            ],
            "aliases": [{"id": a.id, "key": a.key, "excel_name": a.excel_name, "product_id": str(a.product_id)} for a in aliases],
            "cross_sells": [
                {"product_id": str(c.product_id), "related_product_id": str(c.related_product_id), "position": c.position}
                for c in links
            ],
            "flash_sales": [{"id": s.id, "variant_id": str(s.variant_id)} for s in sales],
        }

        if apply:
            REPORTS.mkdir(exist_ok=True)
            path = REPORTS / f"fix-erp-and-deleted-leftovers-{now:%Y%m%d-%H%M%S}.json"
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            report["report_path"] = str(path)
            for r in records:
                r.applied_summary = {
                    **r.applied_summary, "product_id": None,
                    "deleted_product_id": r.applied_summary["product_id"], "detached_at": now.isoformat(),
                }
                r.applied_at = None
            if aliases:
                await db.execute(delete(PriceImportAlias).where(PriceImportAlias.id.in_([a.id for a in aliases])))
            if links:
                await db.execute(delete(ProductCrossSell).where(ProductCrossSell.related_product_id.in_(deleted)))
            if sales:
                await db.execute(update(FlashSale).where(FlashSale.id.in_([s.id for s in sales])).values(is_active=False))
            await db.commit()
    await engine.dispose()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--code", action="append", default=[], help="Write the ERP fix for this product code (repeatable)")
    parser.add_argument("--all-erp", action="store_true", help="Write the ERP fix for every product listed")
    parser.add_argument("--apply", action="store_true", help="Write (default: dry run)")
    args = parser.parse_args()
    report = asyncio.run(run({c.strip().lower() for c in args.code}, args.all_erp, args.apply))
    for row in report["erp"]:
        mark = "WRITTEN" if row["written"] else "listed"
        print(f"ERP {mark}: {row['code']} ({row['sheet_currency']} row {row['record_id'][:8]})")
        print(f"    old {json.dumps(row['old'], ensure_ascii=False)}")
        print(f"    new {json.dumps(row['new'], ensure_ascii=False)}")
    d = report["deleted"]
    print(
        f"Deleted products' leftovers: {len(d['records'])} applied rows, {len(d['aliases'])} aliases, "
        f"{len(d['cross_sells'])} cross-sells, {len(d['flash_sales'])} active flash sales"
        + (" — cleaned" if report["applied"] else " (dry run)")
    )
    for r in d["records"]:
        print(f"    row {r['id'][:8]} {r['code']} applied {r['applied_at'][:16]}")
    if report.get("report_path"):
        print(f"Report: {report['report_path']}")


if __name__ == "__main__":
    main()
