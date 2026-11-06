"""One category made a top category of its own, found by its slug, optionally renamed.

    .venv/bin/python scripts/promote_category.py --slug siseler --name "Nargile Şişeleri" --after marpuclar --dry-run
    .venv/bin/python scripts/promote_category.py --slug siseler --name "Nargile Şişeleri" --after marpuclar --refresh-storefront

Nothing is created or deleted: only the category's parent (none: the top), its
name and its place among the top categories change. Its products, address
(/<slug>: category addresses are flat, so a move changes none), banner, SEO
text and translations stay as they are; its products leave the pages of the
groups it leaves (a group lists its subtree) unless they are listed there
themselves — the report says how many stay.

`--after` puts it right after that top category; without it, last. The other
top categories keep their order among themselves, whatever the panel made it.

Running it again changes nothing. It is one transaction; the way back is the
dump taken before. A write saves what it changed to
scripts/migration_reports/promote-<slug>-<date>.json; with --refresh-storefront
it then asks the storefront to drop its cached catalog (never by default: a
developer's .env points at the live storefront).

The same as promote_root_categories.py (the five of 2026-11-06), for one.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select  # noqa: E402

from app.core.database import async_session, engine  # noqa: E402
from app.models.catalog import Category, Product, ProductCategory  # noqa: E402
from app.services.categories import MAX_CATEGORY_DEPTH  # noqa: E402
from apply_malzemeler_tree import refresh_storefront  # noqa: E402


async def run(slug: str, name: str | None, after: str | None, dry_run: bool, refresh: bool) -> None:
    async with async_session() as db:
        cats = {c.slug: c for c in (await db.execute(select(Category))).scalars()}
        category = cats.get(slug)
        if category is None:
            raise SystemExit(f"Refusing: no category with the slug {slug} — nothing is created")
        if after is not None and (after not in cats or cats[after].parent_id is not None or after == slug):
            raise SystemExit(f"Refusing: --after {after} is not another top category")
        by_id = {c.id: c for c in cats.values()}

        def children(of: Category) -> list[Category]:
            return [c for c in cats.values() if c.parent_id == of.id]

        def subtree(of: Category) -> list[int]:
            return [of.id, *(i for c in children(of) for i in subtree(c))]

        def height(of: Category) -> int:
            return 1 + max((height(c) for c in children(of)), default=0)

        def chain(of: Category) -> list[Category]:
            up = by_id.get(of.parent_id) if of.parent_id else None
            return [*chain(up), up] if up else []

        if height(category) > MAX_CATEGORY_DEPTH:
            raise SystemExit(f"Refusing: {slug} is deeper than the menu's {MAX_CATEGORY_DEPTH} levels")

        # Who is listed where: a link, or the primary alone (rows from before the links).
        members = set((await db.execute(select(ProductCategory.category_id, ProductCategory.product_id))).all())
        members |= set((await db.execute(select(Product.category_id, Product.id).where(Product.category_id.is_not(None)))).all())
        listed: dict[int, set] = {}
        for category_id, product_id in members:
            listed.setdefault(category_id, set()).add(product_id)

        def products_under(ids) -> set:
            return set().union(*(listed.get(i, set()) for i in ids))

        own = subtree(category)
        products = products_under(own)
        groups = chain(category)
        report = {
            "slug": slug,
            "from": " › ".join(g.name for g in groups) or "(en üst seviye)",
            "name_before": category.name,
            "name_after": name or category.name,
            "products": len(products),
            "leaves_groups": [
                {"slug": g.slug, "name": g.name, "products_staying": len(products & products_under(i for i in subtree(g) if i not in own))}
                for g in reversed(groups)
            ],
            "reordered": [],
        }

        category.parent_id = None
        if name:
            category.name = name
        top = sorted((c for c in cats.values() if c.parent_id is None and c is not category), key=lambda c: (c.sort_order, c.name))
        at = next((i + 1 for i, c in enumerate(top) if c.slug == after), len(top))
        for position, c in enumerate([*top[:at], category, *top[at:]]):
            if c.sort_order != position:
                report["reordered"].append({"slug": c.slug, "from": c.sort_order, "to": position})
                c.sort_order = position
        await db.flush()

        changed = bool(groups) or report["name_before"] != report["name_after"] or report["reordered"]
        rename = (
            f"“{report['name_before']}” → “{report['name_after']}”"
            if report["name_before"] != report["name_after"]
            else f"“{report['name_after']}” (adı aynı)"
        )
        print(f"/{slug}  {rename}{'' if changed else '  (zaten böyle)'}")
        print(f"    from: {report['from']}  ·  {report['products']} ürün yeni ana başlıkla birlikte geçiyor")
        staying = "; ".join(f"{g['name']}: {g['products_staying']} kalıyor" for g in report["leaves_groups"]) or "—"
        print(f"    eski grup sayfalarında yine görünen (orada ayrıca listeli): {staying}")
        print("top level, in order:")
        for c in sorted((c for c in cats.values() if c.parent_id is None), key=lambda c: c.sort_order):
            print(f"  {c.sort_order:>2}  {c.name}  /{c.slug}")
        if report["reordered"]:
            moves = ", ".join(f"{r['slug']} {r['from']}→{r['to']}" for r in report["reordered"])
            print(f"reordered: {moves}")

        if dry_run:
            await db.rollback()
            print("dry run: nothing written")
            return
        await db.commit()
        out = Path(__file__).resolve().parent / "migration_reports" / f"promote-{slug}-{datetime.now():%Y%m%d-%H%M%S}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"written; report: {out}")
        if refresh:
            await refresh_storefront()
        else:
            print("storefront cache: not refreshed (--refresh-storefront asks for it; or an admin save)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--slug", required=True, help="the category to make a top category")
    parser.add_argument("--name", help="its new name (default: kept)")
    parser.add_argument("--after", help="the top category it comes right after (default: last)")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    parser.add_argument(
        "--refresh-storefront", action="store_true", help="after writing, drop the storefront's cached catalog (STOREFRONT_REVALIDATE_URL)"
    )
    args = parser.parse_args()

    async def go():
        try:
            await run(args.slug, args.name, args.after, args.dry_run, args.refresh_storefront)
        finally:
            await engine.dispose()

    asyncio.run(go())


if __name__ == "__main__":
    main()
