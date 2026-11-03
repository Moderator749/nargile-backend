"""Give products the cost of the last pricing worksheet applied to them.

Applying a worksheet ("Dosya ve Sitede Ürüne Kaydet") writes its cost to the
product from now on; rows applied before that only wrote prices. This goes
through every applied row, newest first, and gives each product it was applied
to that row's cost — before VAT, in the sheet's currency — on every active
colour. A product applied more than once takes its latest row, as it would
have then. Products never priced on a worksheet are left as they are and
counted, for the reports' "maliyeti eksik" list.

    cd saas_backend
    .venv/bin/python scripts/backfill_product_costs.py --dry-run
    .venv/bin/python scripts/backfill_product_costs.py

Idempotent: a second run finds nothing to change. One transaction: all or
nothing. Writes a JSON report to scripts/migration_reports/.
"""

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session, engine
from app.models.catalog import CurrencyType, Product, ProductVariant
from app.models.pricing import PricingRecord
from app.services.pricing_sheet import cost_before_vat, find_product

REPORTS = Path(__file__).resolve().parent / "migration_reports"


async def backfill(db: AsyncSession) -> dict:
    """Write each worksheet-priced product's latest cost; return what happened."""
    records = (
        await db.scalars(
            select(PricingRecord)
            .where(PricingRecord.applied_at.is_not(None))
            .order_by(PricingRecord.applied_at.desc())
        )
    ).all()
    latest: dict[uuid.UUID, PricingRecord] = {}
    unmatched: list[str] = []
    for record in records:
        product_id = (record.applied_summary or {}).get("product_id")
        if product_id is None:
            product = await find_product(db, record.product_code)
            product_id = product.id if product else None
        if product_id is None:
            unmatched.append(record.product_code)
            continue
        latest.setdefault(uuid.UUID(str(product_id)), record)

    changed: list[dict] = []
    unchanged = 0
    for product_id, record in latest.items():
        sheet = record.sheet
        cost = cost_before_vat(Decimal(sheet["cost"]), sheet["vat_mode"], Decimal(sheet["retail"]["vat_rate"]))
        currency = CurrencyType(sheet["currency"])
        variants = (
            await db.scalars(
                select(ProductVariant).where(
                    ProductVariant.product_id == product_id, ProductVariant.is_active.is_(True)
                )
            )
        ).all()
        stale = [v for v in variants if v.cost_price != cost or CurrencyType(v.cost_currency) != currency]
        if not stale:
            unchanged += 1
            continue
        for v in stale:
            v.cost_price, v.cost_currency = cost, currency
        changed.append({
            "product_id": str(product_id),
            "code": record.product_code,
            "cost": str(cost),
            "currency": currency.value,
            "applied_at": record.applied_at.isoformat(),
        })
    await db.flush()

    products = await db.scalar(select(func.count()).select_from(Product))
    return {
        "products": products,
        "applied_rows": len(records),
        "products_with_a_worksheet": len(latest),
        "changed": len(changed),
        "already_set": unchanged,
        "without_a_worksheet": products - len(latest),
        "unmatched_codes": sorted(set(unmatched)),
        "samples": changed[:10],
    }


async def run(dry_run: bool) -> dict:
    try:
        async with async_session() as db, db.begin():
            summary = await backfill(db)
            if dry_run:
                await db.rollback()
        return {"finished_at": datetime.now().isoformat(timespec="seconds"), "dry_run": dry_run, **summary}
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = parser.parse_args()
    summary = asyncio.run(run(args.dry_run))
    out = REPORTS / f"product-costs-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "samples"}, ensure_ascii=False, indent=2))
    print(f"report: {out}")


if __name__ == "__main__":
    main()
