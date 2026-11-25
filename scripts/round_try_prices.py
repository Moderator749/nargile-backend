"""Stored TRY prices rounded up to whole lira (1163.86 → 1164.00), once.

    .venv/bin/python scripts/round_try_prices.py             # dry run: counts and examples
    .venv/bin/python scripts/round_try_prices.py --apply     # write, then save a report
    .venv/bin/python scripts/round_try_prices.py --apply --refresh-storefront

What it touches, TRY only (a USD/EUR row is converted at the day's rate and
rounded then, by pricing_engine.to_try):
- variant_prices: the RETAIL (Perakende) row whose after-VAT price has kuruş —
  never Bayi, Toptan or Kafe, which keep their kuruş (2026-11-25). The
  price after VAT goes up to the next whole lira and the one before VAT is
  derived from it at the row's own VAT rate, as pricing_engine does.
- flash_sales.sale_price_incl_tax and products.opportunity_price with kuruş.

Whole prices are left alone, so a second run finds nothing to do. Nothing is
written on a dry run. A write saves every old figure to
scripts/migration_reports/round-try-prices-<time>.json first, which is what a
restore would read; with --refresh-storefront it then asks the storefront to
drop its cached catalog (never by default: a developer's .env points at the
live storefront).
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select, text  # noqa: E402

from app.core.database import async_session, engine  # noqa: E402
from app.models.catalog import CurrencyType, Product, VariantPrice  # noqa: E402
from app.models.promotion import FlashSale  # noqa: E402
from app.models.user import CustomerSegment, SegmentType  # noqa: E402
from app.services.pricing_engine import excl_from_incl, whole_lira  # noqa: E402

REPORTS = Path(__file__).resolve().parent / "migration_reports"


def _fractional(amount: Decimal | None) -> bool:
    return amount is not None and amount != whole_lira(amount)


async def run(apply: bool, refresh: bool) -> dict:
    async with async_session() as db:
        # Read and decide first; the writes below are a few quick UPDATEs.
        prices = [
            row
            for row in (
                await db.scalars(
                    select(VariantPrice)
                    .join(CustomerSegment, CustomerSegment.id == VariantPrice.segment_id)
                    .where(VariantPrice.currency == CurrencyType.TRY, CustomerSegment.code == SegmentType.RETAIL)
                )
            ).all()
            if _fractional(row.price_incl_tax)
        ]
        sales = [s for s in (await db.scalars(select(FlashSale))).all() if _fractional(s.sale_price_incl_tax)]
        products = [
            p
            for p in (await db.scalars(select(Product).where(Product.opportunity_price.is_not(None)))).all()
            if _fractional(p.opportunity_price)
        ]

        report = {
            "variant_prices": [
                {
                    "id": row.id,
                    "variant_id": str(row.variant_id),
                    "segment_id": row.segment_id,
                    "vat_rate": str(row.vat_rate),
                    "old_excl": str(row.price_excl_tax),
                    "old_incl": str(row.price_incl_tax),
                    "new_excl": str(excl_from_incl(whole_lira(row.price_incl_tax), row.vat_rate)),
                    "new_incl": str(whole_lira(row.price_incl_tax)),
                }
                for row in prices
            ],
            "flash_sales": [
                {"id": s.id, "variant_id": str(s.variant_id), "old": str(s.sale_price_incl_tax), "new": str(whole_lira(s.sale_price_incl_tax))}
                for s in sales
            ],
            "opportunity_prices": [
                {"id": str(p.id), "code": p.base_code, "old": str(p.opportunity_price), "new": str(whole_lira(p.opportunity_price))}
                for p in products
            ],
        }
        by_segment: dict[int, int] = {}
        for row in prices:
            by_segment[row.segment_id] = by_segment.get(row.segment_id, 0) + 1
        print(f"variant_prices (TRY, kuruşlu): {len(prices)}  · segment başına: {dict(sorted(by_segment.items()))}")
        for entry in report["variant_prices"][:8]:
            print(f"    {entry['old_incl']} → {entry['new_incl']}  (KDV hariç {entry['old_excl']} → {entry['new_excl']})")
        print(f"flash_sales: {len(sales)}  · opportunity_price: {len(products)}")
        for entry in report["flash_sales"][:5] + report["opportunity_prices"][:5]:
            print(f"    {entry['old']} → {entry['new']}")

        total = len(prices) + len(sales) + len(products)
        if not apply or total == 0:
            print("dry run: nothing written" if not apply else "nothing to round: every TRY price is whole")
            return report

        REPORTS.mkdir(exist_ok=True)
        out = REPORTS / f"round-try-prices-{datetime.now():%Y%m%d-%H%M%S}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"old figures saved: {out}")

        # variant_prices is read by every product page: don't queue behind a lock.
        await db.execute(text("SET LOCAL lock_timeout = '5s'"))
        for row in prices:
            row.price_incl_tax = whole_lira(row.price_incl_tax)
            row.price_excl_tax = excl_from_incl(row.price_incl_tax, row.vat_rate)
        for sale in sales:
            sale.sale_price_incl_tax = whole_lira(sale.sale_price_incl_tax)
        for product in products:
            product.opportunity_price = whole_lira(product.opportunity_price)
        await db.commit()
        print(f"written: {total} rows")

    if refresh:
        from apply_malzemeler_tree import refresh_storefront

        await refresh_storefront()
    else:
        print("storefront cache: not refreshed (--refresh-storefront asks for it; or an admin save)")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the rounded prices (default: dry run)")
    parser.add_argument(
        "--refresh-storefront", action="store_true", help="after writing, drop the storefront's cached catalog (STOREFRONT_REVALIDATE_URL)"
    )
    args = parser.parse_args()

    async def go():
        try:
            await run(args.apply, args.refresh_storefront)
        finally:
            await engine.dispose()

    asyncio.run(go())


if __name__ == "__main__":
    main()
