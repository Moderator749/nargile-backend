"""Products soft-deleted by their code, as the admin's "Sil" does.

    .venv/bin/python scripts/soft_delete_products.py --code LL282 --dry-run
    .venv/bin/python scripts/soft_delete_products.py --code LL282 --refresh-storefront

A product is found by its own code (case-insensitive), else by one of its
colours' SKUs; a code that matches no live product, or more than one, is
refused and nothing is written. The product is marked deleted (`is_deleted`,
`deleted_at`) and unpublished; its row, colours, prices and stock stay, so the
order lines that point at it are untouched.

Running it again changes nothing. A write saves what it changed to
scripts/migration_reports/soft-delete-<date>.json; with --refresh-storefront it
then asks the storefront to drop its cached catalog (never by default: a
developer's .env points at the live storefront).
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import func, select  # noqa: E402

from app.core.database import async_session, engine  # noqa: E402
from app.models.catalog import Product, ProductVariant  # noqa: E402
from app.models.order import OrderItem  # noqa: E402
from app.services.product_blueprint import soft_delete  # noqa: E402
from apply_malzemeler_tree import refresh_storefront  # noqa: E402


async def run(codes: list[str], dry_run: bool, refresh: bool) -> None:
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        found: list[Product] = []
        for code in codes:
            key = code.strip().lower()
            by_code = select(Product.id).where(func.lower(Product.base_code) == key)
            by_sku = select(ProductVariant.product_id).where(func.lower(ProductVariant.sku) == key)
            products = (await db.scalars(select(Product).where(Product.id.in_(by_code.union(by_sku))))).all()
            live = [p for p in products if not p.is_deleted]
            if not live:
                if products:
                    print(f"{code}: zaten silinmiş ({', '.join(p.name for p in products)})")
                    continue
                raise SystemExit(f"Refusing: no product with the code {code} — nothing written")
            if len(live) > 1:
                names = "; ".join(f"{p.base_code} {p.name}" for p in live)
                raise SystemExit(f"Refusing: {code} matches {len(live)} products ({names}) — nothing written")
            found.append(live[0])

        report = []
        for product in found:
            orders = await db.scalar(
                select(func.count(func.distinct(OrderItem.order_id)))
                .join(ProductVariant, ProductVariant.id == OrderItem.variant_id)
                .where(ProductVariant.product_id == product.id)
            )
            print(f"{product.base_code}  “{product.name}”  /{product.slug}")
            print(f"    yayında: {'evet' if product.is_published else 'hayır'}  ·  {orders} siparişte geçiyor (satırlar korunur)")
            report.append({
                "id": str(product.id), "code": product.base_code, "name": product.name, "slug": product.slug,
                "was_published": product.is_published, "orders": orders,
            })
            soft_delete(product, now)
        await db.flush()

        if dry_run or not report:
            await db.rollback()
            print("dry run: nothing written" if dry_run else "nothing to delete")
            return
        await db.commit()
        out = Path(__file__).resolve().parent / "migration_reports" / f"soft-delete-{datetime.now():%Y%m%d-%H%M%S}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps({"deleted_at": now.isoformat(), "products": report}, ensure_ascii=False, indent=1))
        print(f"written; report: {out}")
        if refresh:
            await refresh_storefront()
        else:
            print("storefront cache: not refreshed (--refresh-storefront asks for it; or an admin save)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--code", action="append", required=True, help="a product code or SKU; repeat for more")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    parser.add_argument(
        "--refresh-storefront", action="store_true", help="after writing, drop the storefront's cached catalog (STOREFRONT_REVALIDATE_URL)"
    )
    args = parser.parse_args()

    async def go():
        try:
            await run(args.code, args.dry_run, args.refresh_storefront)
        finally:
            await engine.dispose()

    asyncio.run(go())


if __name__ == "__main__":
    main()
