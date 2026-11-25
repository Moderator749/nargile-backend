"""Give Bayi, Toptan and Kafe back the kuruş the whole-lira rounding took (once).

    .venv/bin/python scripts/restore_b2b_kurus.py             # dry run: the plan
    .venv/bin/python scripts/restore_b2b_kurus.py --apply     # write, after saving every old figure

Since 2026-11-25 only RETAIL is kept in whole lira; the business groups keep
their kuruş. Two things rounded them before that, and each left what is
needed to undo it:

1. scripts/round_try_prices.py (2026-11-23) saved every row's old figures in
   migration_reports/round-try-prices-*.json. A business row that still holds
   the rounded figure it wrote gets its old one back.
2. A worksheet ("Fiyat Hesaplama Cetveli") applied since then wrote its TRY
   business prices rounded up. Its saved sheet still has each group's
   computed price; a row that still holds what the apply wrote (its
   applied_summary) gets that price to the kuruş, as the apply now writes it.
   Only the product's latest apply counts, and it wins over (1).

A row changed since (by hand, a later sheet, an import) is left alone and
listed. Retail is never touched. Nothing is written on a dry run; a write
saves the plan with every old figure to
migration_reports/restore-b2b-kurus-<time>.json first, and sets a 5 s
lock_timeout. A second run finds nothing to do.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text  # noqa: E402

from app.core.database import async_session, engine  # noqa: E402
from app.models.catalog import CurrencyType, ProductVariant, VariantPrice  # noqa: E402
from app.models.pricing import PricingRecord  # noqa: E402
from app.models.user import CustomerSegment, SegmentType  # noqa: E402
from app.services.pricing_engine import whole_lira  # noqa: E402
from app.services.pricing_sheet import _both_sides  # noqa: E402

REPORTS = Path(__file__).resolve().parent / "migration_reports"
BUSINESS = (SegmentType.DEALER, SegmentType.WHOLESALE, SegmentType.CAFE)
# The whole-lira code went live on 2026-11-23; nothing applied earlier was rounded.
SINCE = datetime(2026, 11, 23, tzinfo=timezone.utc)


def _d(value) -> Decimal:
    return Decimal(str(value))


async def plan(db, report: Path | None) -> dict:
    segments = {SegmentType(code): sid for sid, code in (await db.execute(select(CustomerSegment.id, CustomerSegment.code))).all()}
    business = {segments[s]: s for s in BUSINESS if s in segments}
    rows = {
        r.id: r
        for r in (
            await db.scalars(
                select(VariantPrice).where(
                    VariantPrice.segment_id.in_(list(business)), VariantPrice.currency == CurrencyType.TRY
                )
            )
        ).all()
    }
    changes: dict[int, dict] = {}
    skipped: list[dict] = []

    # (2) Worksheets applied since the rounding: the latest per product.
    records = (
        await db.scalars(
            select(PricingRecord)
            .where(PricingRecord.applied_at >= SINCE, PricingRecord.applied_summary.is_not(None))
            .order_by(PricingRecord.applied_at.desc())
        )
    ).all()
    latest: dict[str, PricingRecord] = {}
    for record in records:
        latest.setdefault((record.applied_summary or {}).get("product_id"), record)
    latest.pop(None, None)
    variants_of: dict[str, list] = {}
    if latest:
        for v in (
            await db.scalars(select(ProductVariant).where(ProductVariant.product_id.in_(list(latest))))
        ).all():
            variants_of.setdefault(str(v.product_id), []).append(v.id)
    sheet_rows: set[int] = set()
    for product_id, record in latest.items():
        sheet = record.sheet or {}
        vat_rate = _d((sheet.get("retail") or {}).get("vat_rate", "20"))
        columns = {c.get("segment"): c for c in sheet.get("columns", []) if c.get("segment")}
        for written in record.applied_summary.get("prices", []):
            segment = SegmentType(written["segment"])
            if segment not in BUSINESS or written["currency"] != CurrencyType.TRY.value:
                continue
            col = columns.get(segment.value)
            if col is None or sheet.get("currency") != CurrencyType.TRY.value:
                continue
            excl, incl = _both_sides(_d(col["price"]), sheet.get("vat_mode", "EXCL"), vat_rate)
            put = (_d(written["price_excl_tax"]), _d(written["price_incl_tax"]))
            if put == (excl, incl):
                continue  # it had no kuruş to lose
            if whole_lira(incl) != put[1]:
                skipped.append({"why": "sheet re-saved since its apply", "record": str(record.id),
                                "code": record.product_code, "segment": segment.value})
                continue
            seg_id = segments[segment]
            for row in rows.values():
                if row.segment_id != seg_id or row.variant_id not in variants_of.get(product_id, []):
                    continue
                sheet_rows.add(row.id)
                current = (row.price_excl_tax, row.price_incl_tax)
                if current != put:
                    skipped.append({"why": "changed since the sheet's apply", "id": row.id, "code": record.product_code,
                                    "segment": segment.value, "current": [str(x) for x in current]})
                    continue
                changes[row.id] = {
                    "id": row.id, "variant_id": str(row.variant_id), "segment": segment.value,
                    "source": f"sheet {record.id}", "code": record.product_code,
                    "old_excl": str(current[0]), "old_incl": str(current[1]),
                    "new_excl": str(excl), "new_incl": str(incl),
                }

    # (1) The one-off rounding's report.
    if report is not None:
        for saved in json.loads(report.read_text())["variant_prices"]:
            if saved["segment_id"] not in business or saved["id"] in sheet_rows:
                continue
            row = rows.get(saved["id"])
            if row is None:
                skipped.append({"why": "row gone or no longer TRY", "id": saved["id"]})
                continue
            current = (row.price_excl_tax, row.price_incl_tax)
            if current != (_d(saved["new_excl"]), _d(saved["new_incl"])):
                skipped.append({"why": "changed since the rounding", "id": row.id,
                                "segment": business[row.segment_id].value, "current": [str(x) for x in current],
                                "rounded": [saved["new_excl"], saved["new_incl"]]})
                continue
            changes[row.id] = {
                "id": row.id, "variant_id": str(row.variant_id), "segment": business[row.segment_id].value,
                "source": report.name,
                "old_excl": str(current[0]), "old_incl": str(current[1]),
                "new_excl": saved["old_excl"], "new_incl": saved["old_incl"],
            }
    return {"changes": list(changes.values()), "skipped": skipped, "sheets": len(latest)}


async def run(apply: bool, report: Path | None) -> dict:
    async with async_session() as db:
        found = await plan(db, report)
        if apply and found["changes"]:
            REPORTS.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            (REPORTS / f"restore-b2b-kurus-{stamp}.json").write_text(json.dumps(found, indent=1, ensure_ascii=False))
            await db.execute(text("SET LOCAL lock_timeout = '5s'"))
            for change in found["changes"]:
                row = await db.get(VariantPrice, change["id"])
                row.price_excl_tax, row.price_incl_tax = _d(change["new_excl"]), _d(change["new_incl"])
            await db.commit()
    await engine.dispose()
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, help="round-try-prices-*.json (default: the newest)")
    args = parser.parse_args()
    report = args.report or max(REPORTS.glob("round-try-prices-*.json"), default=None)
    found = asyncio.run(run(args.apply, report))
    by_source: dict[str, int] = {}
    for c in found["changes"]:
        key = "sheet" if c["source"].startswith("sheet") else "rounding report"
        by_source[key] = by_source.get(key, 0) + 1
    print(f"{'Restored' if args.apply else 'Would restore'} {len(found['changes'])} business TRY rows {by_source}")
    print(f"Skipped {len(found['skipped'])}; applied sheets since {SINCE.date()}: {found['sheets']}")
    for c in found["changes"][:15]:
        print(f"  {c.get('code') or c['variant_id'][:8]} {c['segment']}: {c['old_excl']}/{c['old_incl']} → {c['new_excl']}/{c['new_incl']}")
    for s in found["skipped"][:15]:
        print("  skip", s)


if __name__ == "__main__":
    main()
