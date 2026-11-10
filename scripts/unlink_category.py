"""Every product taken out of one category, found by its slug; their other categories kept.

    .venv/bin/python scripts/unlink_category.py --slug aksesuarlar --dry-run
    .venv/bin/python scripts/unlink_category.py --slug aksesuarlar --refresh-storefront

For a category layer the admin is about to remove (Aksesuarlar, 2026-11-10):
its products are tagged with it *and* with a subcategory (/koz-masalari…), so
once they are out of it the category is empty and the panel can delete it.

Nothing is deleted but the product's link to that category: the product, its
other categories and the category itself stay. Membership is the primary
(`products.category_id`) and the links (product_categories), and both are
cleared — soft-deleted products too, since the panel's delete counts them.
A product whose primary was this category gets its next one as primary (the
breadcrumb's); the links are renumbered from 0, primary first, as the sheet
writes them.

A product listed under this category alone is left as it is and named in the
output: taking it out would leave it without a category. Place it in the panel
(or give it another category) and run this again.

Running it again changes nothing. It is one transaction; the way back is the
dump taken before. A write saves what it changed to
scripts/migration_reports/unlink-<slug>-<date>.json; with --refresh-storefront
it then asks the storefront to drop its cached catalog.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import delete, or_, select  # noqa: E402

from app.core.database import async_session, engine  # noqa: E402
from app.models.catalog import Category, Product, ProductCategory  # noqa: E402
from apply_malzemeler_tree import refresh_storefront  # noqa: E402


async def run(slug: str, dry_run: bool, refresh: bool) -> None:
    async with async_session() as db:
        category = await db.scalar(select(Category).where(Category.slug == slug))
        if category is None:
            raise SystemExit(f"Refusing: no category with the slug {slug}")
        names = {c.id: c.name for c in (await db.execute(select(Category))).scalars()}

        linked = select(ProductCategory.product_id).where(ProductCategory.category_id == category.id)
        products = (
            await db.execute(
                select(Product)
                .where(or_(Product.category_id == category.id, Product.id.in_(linked)))
                .order_by(Product.name)
            )
        ).scalars().all()
        links: dict = {}
        for link in (
            await db.execute(
                select(ProductCategory)
                .where(ProductCategory.product_id.in_([p.id for p in products]))
                .order_by(ProductCategory.product_id, ProductCategory.position)
            )
        ).scalars():
            links.setdefault(link.product_id, []).append(link.category_id)

        report = {"slug": slug, "category_id": category.id, "unlinked": [], "left_alone": []}
        for product in products:
            # The categories as the sheet reads them: the links, the primary first if missing.
            before = list(links.get(product.id, []))
            if product.category_id is not None and product.category_id not in before:
                before.insert(0, product.category_id)
            after = [cid for cid in before if cid != category.id]
            row = {
                "id": str(product.id),
                "code": product.base_code,
                "name": product.name,
                "deleted": product.is_deleted,
                "before": before,
                "after": after,
                "primary_before": product.category_id,
            }
            if not after:
                report["left_alone"].append(row)
                continue
            product.category_id = after[0]
            await db.execute(delete(ProductCategory).where(ProductCategory.product_id == product.id))
            db.add_all(ProductCategory(product_id=product.id, category_id=cid, position=i) for i, cid in enumerate(after))
            row["primary_after"] = after[0]
            report["unlinked"].append(row)
        await db.flush()

        remaining = await db.scalar(
            select(Product.id).where(or_(Product.category_id == category.id, Product.id.in_(linked))).limit(1)
        )

        print(f"/{slug} “{category.name}”: {len(products)} ürün listeli")
        for row in report["unlinked"]:
            mark = "  (silinmiş)" if row["deleted"] else ""
            new_primary = "" if row["primary_before"] == row["primary_after"] else f"  · ana kategori → {names[row['primary_after']]}"
            print(f"  - {row['code']}  {row['name']}{mark}: {', '.join(names[c] for c in row['after'])}{new_primary}")
        if report["left_alone"]:
            print(f"left alone (only in /{slug} — would have no category):")
            for row in report["left_alone"]:
                print(f"  ! {row['code']}  {row['name']}{'  (silinmiş)' if row['deleted'] else ''}")
        print(
            f"unlinked {len(report['unlinked'])}, left alone {len(report['left_alone'])}; "
            f"/{slug} {'still has products' if remaining else 'now empty'}"
        )

        if dry_run:
            await db.rollback()
            print("dry run: nothing written")
            return
        await db.commit()
        out = Path(__file__).resolve().parent / "migration_reports" / f"unlink-{slug}-{datetime.now():%Y%m%d-%H%M%S}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"written; report: {out}")
        if refresh:
            await refresh_storefront()
        else:
            print("storefront cache: not refreshed (--refresh-storefront asks for it; or an admin save)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--slug", required=True, help="the category to take every product out of")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    parser.add_argument(
        "--refresh-storefront", action="store_true", help="after writing, drop the storefront's cached catalog (STOREFRONT_REVALIDATE_URL)"
    )
    args = parser.parse_args()

    async def go():
        try:
            await run(args.slug, args.dry_run, args.refresh_storefront)
        finally:
            await engine.dispose()

    asyncio.run(go())


if __name__ == "__main__":
    main()
