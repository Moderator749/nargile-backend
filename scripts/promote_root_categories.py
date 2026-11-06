"""Five shelves of 2026-11-06 made top categories of their own, with fuller names.

    Közlükler          (Nargile Malzemeleri › Aksesuarlar)       → "Isı Yönetim Araçları"
    Marpuçlar          (Nargile Malzemeleri › Aksesuarlar)       → "Nargile Marpuçları"
    Kömürler           (Nargile Malzemeleri › Sarf Malzemeleri)  → "Nargile Kömürleri"
    Bitkisel Ürünler   (Nargile Malzemeleri › Sarf Malzemeleri)  → name kept
    Lüleler            (Nargile Malzemeleri › Aksesuarlar)       → "Nargile Lüleleri"

Nothing is created or deleted: each category is found by its slug, and only
its parent (none: the top) and its name change. Its products, address
(/<slug> — category addresses are flat, so a move changes none), banner, SEO
text and translations stay as they are. Its products leave the pages of the
groups it leaves (a group lists its subtree), unless they are listed there
themselves.

The top categories are put in the storefront's menu order (TOP_NAV): Fırsat
Ürünler, Nargile Takımları, the five above, Nargile Malzemeleri, Oyun &
Eğlence; any other top category after them, as it was. The panel can reorder
them afterwards.

    .venv/bin/python scripts/promote_root_categories.py --dry-run   # show, write nothing
    .venv/bin/python scripts/promote_root_categories.py             # write (pg_dump first)

Running it again changes nothing. It is one transaction; the way back is the
dump taken before. A write saves what it changed to
scripts/migration_reports/promote-roots-<date>.json; with --refresh-storefront
it then asks the storefront to drop its cached catalog (never by default: a
developer's .env points at the live storefront).
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

# slug → its new name (None: kept), in the order they take among the top categories.
PROMOTED = [
    ("luleler", "Nargile Lüleleri"),
    ("marpuclar", "Nargile Marpuçları"),
    ("kozlukler", "Isı Yönetim Araçları"),
    ("komurler", "Nargile Kömürleri"),
    ("bitkisel-urunler", None),
]
# The top level, in the storefront's menu order; the promoted go after BEFORE.
BEFORE = ("kampanyalar", "nargile-takimlari")
AFTER = ("malzemeler", "oyun-eglence")


async def run(dry_run: bool, refresh: bool) -> None:
    async with async_session() as db:
        cats = {c.slug: c for c in (await db.execute(select(Category))).scalars()}
        missing = [slug for slug, _ in PROMOTED if slug not in cats]
        if missing:
            raise SystemExit(f"Refusing: no category with the slug {', '.join(missing)} — nothing is created")
        by_id = {c.id: c for c in cats.values()}

        def path(category: Category) -> str:
            names = []
            up = by_id.get(category.parent_id) if category.parent_id else None
            while up is not None:
                names.insert(0, up.name)
                up = by_id.get(up.parent_id) if up.parent_id else None
            return " › ".join(names) or "(en üst seviye)"

        def subtree(category: Category) -> list[int]:
            ids, todo = [], [category.id]
            while todo:
                current = todo.pop()
                ids.append(current)
                todo.extend(c.id for c in cats.values() if c.parent_id == current)
            return ids

        def height(category: Category) -> int:
            return 1 + max((height(c) for c in cats.values() if c.parent_id == category.id), default=0)

        # Who is listed where: a link, or the primary alone (rows from before the links).
        members = set((await db.execute(select(ProductCategory.category_id, ProductCategory.product_id))).all())
        members |= set((await db.execute(select(Product.category_id, Product.id).where(Product.category_id.is_not(None)))).all())
        listed: dict[int, set] = {}
        for category_id, product_id in members:
            listed.setdefault(category_id, set()).add(product_id)

        def products_under(category: Category) -> set:
            return set().union(*(listed.get(i, set()) for i in subtree(category)))

        # Everything measured on the tree as it is, before anything moves; a
        # group's products afterwards are its subtree less all five.
        leaving = {i for slug, _ in PROMOTED for i in subtree(cats[slug])}
        report: dict = {"promoted": [], "reordered": []}
        for slug, new_name in PROMOTED:
            category = cats[slug]
            if height(category) > MAX_CATEGORY_DEPTH:
                raise SystemExit(f"Refusing: {slug} is deeper than the menu's {MAX_CATEGORY_DEPTH} levels")
            products = products_under(category)
            # The groups it leaves, and how many of its products stay on each
            # because they are listed there (or elsewhere under it) themselves.
            groups = []
            up = by_id.get(category.parent_id) if category.parent_id else None
            while up is not None:
                rest = set().union(*(listed.get(i, set()) for i in subtree(up) if i not in leaving))
                groups.append({"slug": up.slug, "name": up.name, "products_staying": len(products & rest)})
                up = by_id.get(up.parent_id) if up.parent_id else None
            row = {
                "slug": slug,
                "from": path(category),
                "name_before": category.name,
                "name_after": new_name or category.name,
                "products": len(products),
                "leaves_groups": groups,
                "changed": category.parent_id is not None or (new_name is not None and category.name != new_name),
            }
            report["promoted"].append(row)
        for slug, new_name in PROMOTED:
            cats[slug].parent_id = None
            if new_name:
                cats[slug].name = new_name

        top = [c for c in cats.values() if c.parent_id is None]
        promoted = [slug for slug, _ in PROMOTED]
        order = [*BEFORE, *promoted, *AFTER]
        rest = sorted((c for c in top if c.slug not in order), key=lambda c: (c.sort_order, c.name))
        for position, category in enumerate([*(cats[s] for s in order if s in cats), *rest]):
            if category.sort_order != position:
                report["reordered"].append({"slug": category.slug, "from": category.sort_order, "to": position})
                category.sort_order = position
        await db.flush()

        print("promoted to the top:")
        for row in report["promoted"]:
            rename = f"“{row['name_before']}” → “{row['name_after']}”" if row["name_before"] != row["name_after"] else f"“{row['name_after']}” (adı aynı)"
            left = "; ".join(f"{g['name']}: {g['products_staying']} kalıyor" for g in row["leaves_groups"]) or "—"
            state = "" if row["changed"] else "  (zaten böyle)"
            print(f"  /{row['slug']:<18} {rename}{state}")
            print(f"      from: {row['from']}  ·  {row['products']} ürün yeni ana başlıkla birlikte geçiyor")
            print(f"      eski grup sayfalarında yine görünen (orada ayrıca listeli): {left}")
        print("top level, in order:")
        for category in sorted((c for c in cats.values() if c.parent_id is None), key=lambda c: c.sort_order):
            print(f"  {category.sort_order:>2}  {category.name}  /{category.slug}")
        if report["reordered"]:
            moves = ", ".join(f"{r['slug']} {r['from']}→{r['to']}" for r in report["reordered"])
            print(f"reordered: {moves}")

        if dry_run:
            await db.rollback()
            print("dry run: nothing written")
            return
        await db.commit()
        out = Path(__file__).resolve().parent / "migration_reports" / f"promote-roots-{datetime.now():%Y%m%d-%H%M%S}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"written; report: {out}")
        if refresh:
            await refresh_storefront()
        else:
            print("storefront cache: not refreshed (--refresh-storefront asks for it; or an admin save)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    # Opt-in: the URL in a developer's .env is the live storefront's, and a run
    # on a local copy must not reach it.
    parser.add_argument(
        "--refresh-storefront", action="store_true", help="after writing, drop the storefront's cached catalog (STOREFRONT_REVALIDATE_URL)"
    )
    args = parser.parse_args()

    async def go():
        try:
            await run(args.dry_run, args.refresh_storefront)
        finally:
            await engine.dispose()

    asyncio.run(go())


if __name__ == "__main__":
    main()
