"""One "Sitede" record per product, once (2026-11-25).

    .venv/bin/python scripts/supersede_applied_records.py            # dry run
    .venv/bin/python scripts/supersede_applied_records.py --apply    # write, after saving every old summary

Since 2026-11-25 applying a worksheet marks the product's earlier applied
records superseded (admin_pricing._mark_applied). Records applied before that
all still claim the product. For each product, every applied record but the
newest gets `superseded_at` / `superseded_by` in its applied_summary, as the
endpoint now writes them; `applied_at` and the rest are kept.

Also: scripts/restore_b2b_kurus.py gave Bayi, Toptan and Kafe back their kuruş
on rows a worksheet had written rounded up, but the worksheet's summary still
says the rounded figure, so the panel would flag it as changed on the site.
The figures it restored are written into those records' summaries too (from
the newest migration_reports/restore-b2b-kurus-*.json).

Nothing is written on a dry run; --apply saves every summary it changes to
migration_reports/supersede-applied-<time>.json first. A second run finds
nothing to do.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text  # noqa: E402

from app.core.database import async_session, engine  # noqa: E402
from app.models.pricing import PricingRecord  # noqa: E402

REPORTS = Path(__file__).resolve().parent / "migration_reports"


def _restored(report: Path | None) -> dict[str, dict[str, tuple[str, str]]]:
    """Per worksheet record id, segment → the (excl, incl) the restore wrote."""
    if report is None:
        return {}
    out: dict[str, dict[str, tuple[str, str]]] = {}
    for change in json.loads(report.read_text())["changes"]:
        source = change.get("source", "")
        if source.startswith("sheet "):
            out.setdefault(source.removeprefix("sheet "), {})[change["segment"]] = (change["new_excl"], change["new_incl"])
    return out


async def run(apply: bool, report: Path | None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    restored = _restored(report)
    async with async_session() as db:
        await db.execute(text("SET LOCAL lock_timeout = '5s'"))
        records = (
            await db.scalars(
                select(PricingRecord)
                .where(PricingRecord.applied_at.is_not(None))
                .order_by(PricingRecord.applied_at.desc(), PricingRecord.id)
            )
        ).all()
        newest: dict[str, PricingRecord] = {}
        changes: list[dict] = []
        for record in records:
            summary = dict(record.applied_summary or {})
            product_id = summary.get("product_id")
            if not product_id:
                continue
            new = dict(summary)
            if product_id not in newest:
                newest[product_id] = record
            elif not summary.get("superseded_at"):
                new |= {"superseded_at": now, "superseded_by": str(newest[product_id].id)}
            fixed = restored.get(str(record.id), {})
            if fixed:
                prices = []
                for price in summary.get("prices", []):
                    if price.get("segment") in fixed and price.get("currency") == "TRY":
                        excl, incl = fixed[price["segment"]]
                        price = {**price, "price_excl_tax": excl, "price_incl_tax": incl}
                    prices.append(price)
                new["prices"] = prices
            if new != summary:
                changes.append({"id": str(record.id), "code": record.product_code, "old": summary, "new": new})
                if apply:
                    record.applied_summary = new
        if apply and changes:
            REPORTS.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            (REPORTS / f"supersede-applied-{stamp}.json").write_text(json.dumps(changes, indent=1, ensure_ascii=False))
            await db.commit()
    await engine.dispose()
    return {"changes": changes, "products": len(newest)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore-report", type=Path, help="restore-b2b-kurus-*.json (default: the newest)")
    args = parser.parse_args()
    report = args.restore_report or max(REPORTS.glob("restore-b2b-kurus-*.json"), default=None)
    found = asyncio.run(run(args.apply, report))
    superseded = sum(1 for c in found["changes"] if "superseded_at" in c["new"] and "superseded_at" not in c["old"])
    resynced = sum(1 for c in found["changes"] if c["new"].get("prices") != c["old"].get("prices"))
    verb = "Changed" if args.apply else "Would change"
    print(f"{verb} {len(found['changes'])} records: {superseded} superseded, {resynced} summaries given the restored kuruş")
    print(f"Products with an applied record: {found['products']}; restore report: {report.name if report else None}")
    for c in found["changes"][:12]:
        print(f"  {c['code']}: {'superseded' if 'superseded_at' in c['new'] else ''} {'prices' if c['new'].get('prices') != c['old'].get('prices') else ''}")


if __name__ == "__main__":
    main()
