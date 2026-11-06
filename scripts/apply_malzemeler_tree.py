"""The "Nargile Malzemeleri" tree of 2026-11-06: the four groups of the
2026-10-29 taxonomy (apply_category_taxonomy.py) move under Nargile
Malzemeleri, and each product of their kinds goes onto its shelf by its name.

    Nargile Malzemeleri (malzemeler)
    ├── Sarf Malzemeleri    Folyolar, Bez Bantlar
    ├── Aksesuarlar         Nargile Çantaları, Köz Tavaları ve Bacaları, Köz Maşaları,
    │                       Rüzgarlıklar, Temizlik Fırçaları, Led Işıklar, Deliciler,
    │                       Elekler, Çatallar, Lüle Tıpaları, Pompalar
    ├── Yakıcılar           Elektrikli Ocaklar, Gazlı Ocaklar, Gazlar ve Pürmüzler,
    │                       Çakmaklar (switched off)
    └── Yedek Parçalar      Izgaralar, Rezistanslar, Lüle ve Marpuç Bağlantıları, Contalar,
                            Susturucular, Yapıştırıcılar, Brülörler, Hortum Yayları

- Nothing is deleted and recreated: a group or shelf that exists is found by
  its slug and moved, keeping its address, banner, SEO text and
  translations. Only what is missing is created.
- The groups' other shelves (Kömürler, Sipsiler, Lüleler, Marpuçlar,
  Şişeler…) stay where they are, with their products; they come first in
  nothing and are listed after the shelves above.
- "Sarf Tüketim Malzemeleri" is named "Sarf Malzemeleri" (its address stays).
- Gazlar and Pürmüzler become one shelf: "gazlar", renamed "Gazlar ve
  Pürmüzler", takes Pürmüzler's products, and the empty "purmuzler" is
  deleted (its address 301s to /gazlar: RETIRED_CATEGORIES in the
  storefront, reserved in app.core.slugs).
- Products: complete sets (is_set) are never touched. Every other product,
  published or not, is matched by its name with the 2026-10-29 RULES (first
  match wins); one whose kind is a shelf above is put on it, as the October
  script puts it (that shelf first, plus "Fırsat Ürünler" / "Nargile Malzemeleri"
  if it was listed there, and any other shelf it is listed on outside this
  tree, such as its brand's page). One whose kind is another shelf is left as it is;
  one no rule matches is left as it is and printed, so its name can be
  fixed. (Unlike October, a part on a set brand's shelf keeps that listing.)

    .venv/bin/python scripts/apply_malzemeler_tree.py --dry-run   # show, write nothing
    .venv/bin/python scripts/apply_malzemeler_tree.py             # write (pg_dump first)

Running it again changes nothing. Each write saves what it changed, product
by product, to scripts/migration_reports/malzemeler-tree-<date>.json; all of
it is one transaction, and the way back is the dump taken before. With
--refresh-storefront it then asks the storefront to drop its cached catalog
(STOREFRONT_REVALIDATE_URL) — never by default, as a developer's .env points
at the live storefront.

Note: apply_category_taxonomy.py puts the four groups back at the top if run
again; it is the October tree, kept for its history and its RULES.
"""

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import async_session, engine  # noqa: E402
from app.models.catalog import Category, CategoryProductOrder, Product, ProductCategory  # noqa: E402
from app.services.categories import MAX_CATEGORY_DEPTH  # noqa: E402
from apply_category_taxonomy import KEPT_LISTINGS, RULES, SET_SHELVES, TO_GROUP, is_set, low  # noqa: E402

ROOT = ("malzemeler", "Nargile Malzemeleri", "Hookah Accessories", "مستلزمات الشيشة")

# slug, name, English, Arabic — the names are only written for what does not
# exist yet (Gazlar's rename aside); what exists keeps its own.
TREE = [
    ("sarf-malzemeleri", "Sarf Malzemeleri", "Consumables", "مستلزمات استهلاكية", [
        ("folyolar", "Folyolar", "Hookah Foil", "ورق ألمنيوم للشيشة"),
        ("bez-bantlar", "Bez Bantlar", "Cloth Tapes", "أشرطة قماشية"),
    ]),
    ("aksesuarlar", "Aksesuarlar", "Accessories", "إكسسوارات", [
        ("nargile-cantalari", "Nargile Çantaları", "Hookah Bags", "حقائب شيشة"),
        ("koz-tavalari-ve-bacalari", "Köz Tavaları ve Bacaları", "Charcoal Pans and Chimneys", "صحون ومداخن الفحم"),
        ("koz-masalari", "Köz Maşaları", "Charcoal Tongs", "ملاقط الفحم"),
        ("ruzgarliklar", "Rüzgarlıklar", "Wind Covers", "أغطية الرياح"),
        ("temizlik-fircalari", "Temizlik Fırçaları", "Cleaning Brushes", "فرش تنظيف"),
        ("led-isiklar", "Led Işıklar", "LED Lights", "إضاءة LED"),
        ("deliciler", "Deliciler", "Foil Pokers", "أدوات ثقب الورق"),
        ("elekler", "Elekler", "Screens", "شبكات"),
        ("catallar", "Çatallar", "Hookah Forks", "شوك الشيشة"),
        ("lule-tipalari", "Lüle Tıpaları", "Bowl Plugs", "سدادات الرأس"),
        ("pompalar", "Pompalar", "Pumps", "مضخات"),
    ]),
    ("yakicilar", "Yakıcılar", "Burners and Lighters", "مواقد وولاعات", [
        ("elektrikli-ocaklar", "Elektrikli Ocaklar", "Electric Burners", "مواقد كهربائية"),
        ("gazli-ocaklar", "Gazlı Ocaklar", "Gas Burners", "مواقد غاز"),
        ("gazlar", "Gazlar ve Pürmüzler", "Lighter Gas and Torches", "غاز الولاعات وولاعات الشعلة"),
        ("cakmaklar", "Çakmaklar", "Lighters", "ولاعات"),
    ]),
    ("yedek-parca", "Yedek Parçalar", "Spare Parts", "قطع غيار", [
        ("izgaralar", "Izgaralar", "Grates", "شبكات الموقد"),
        ("rezistanslar", "Rezistanslar", "Heating Elements", "مقاومات تسخين"),
        ("lule-ve-marpuc-baglantilari", "Lüle ve Marpuç Bağlantıları", "Bowl and Hose Connectors", "وصلات الرأس واللي"),
        ("contalar", "Contalar", "Grommets", "جلدات"),
        ("susturucular", "Susturucular", "Diffusers", "ناشرات"),
        ("yapistiricilar", "Yapıştırıcılar", "Adhesives", "مواد لاصقة"),
        ("brulorler", "Brülörler", "Burner Heads", "رؤوس المواقد"),
        ("hortum-yaylari", "Hortum Yayları", "Hose Springs", "نوابض اللي"),
    ]),
]
# Pürmüzler's products and its rule go to Gazlar; the shelf itself is deleted once empty.
MERGED = {"purmuzler": "gazlar"}
# What exists keeps its name, but for these: the tree's own names.
RENAMED = {"gazlar", "sarf-malzemeleri"}
SWITCHED_OFF = {"cakmaklar"}

LEAVES = {shelf[0] for *_, shelves in TREE for shelf in shelves}


def kind_of(name: str) -> str | None:
    """The shelf the name says, by the October rules (first match wins); None when none matches."""
    n = low(name)
    for pattern, slug in TO_GROUP:
        if re.search(pattern, n.strip()):
            return slug
    for pattern, slug in RULES:
        if re.search(pattern, n):
            if slug is None:  # a sipsi: its own shelves, not this tree's
                return "sipsiler"
            return MERGED.get(slug, slug)
    return None


def with_names(category: Category, en: str, ar: str) -> dict:
    """The category's translations with these names, whatever else they hold (descriptions) kept."""
    translations = dict(category.translations or {})
    for lang, name in (("en", en), ("ar", ar)):
        translations[lang] = {**(translations.get(lang) or {}), "name": name}
    return translations


async def run(dry_run: bool, refresh: bool) -> None:
    async with async_session() as db:
        cats = {c.slug: c for c in (await db.execute(select(Category))).scalars()}
        report: dict = {"created": [], "moved": [], "renamed": [], "switched_off": [], "deleted": [], "products": [], "unmatched": []}

        def ensure(slug: str, name: str, en: str, ar: str) -> Category:
            category = cats.get(slug)
            if category is None:
                category = Category(name=name, slug=slug, translations={"en": {"name": en}, "ar": {"name": ar}})
                db.add(category)
                cats[slug] = category
                report["created"].append(slug)
            return category

        # 1. The tree. The root keeps its place among the top categories.
        root = ensure(*ROOT)
        await db.flush()
        for g_order, (g_slug, g_name, g_en, g_ar, shelves) in enumerate(TREE):
            group = ensure(g_slug, g_name, g_en, g_ar)
            if g_slug in RENAMED and group.name != g_name:
                report["renamed"].append({"slug": g_slug, "from": group.name, "to": g_name})
                group.name = g_name
                group.translations = with_names(group, g_en, g_ar)
            if group.parent_id != root.id or group.sort_order != g_order:
                report["moved"].append({"slug": g_slug, "parent": ROOT[0], "sort_order": g_order})
            group.parent_id, group.sort_order = root.id, g_order
            await db.flush()
            listed = [s[0] for s in shelves]
            # The group's other shelves after these, in the order they had.
            others = sorted(
                (c for c in cats.values() if c.parent_id == group.id and c.slug not in listed and c.slug not in MERGED),
                key=lambda c: (c.sort_order, c.name),
            )
            for s_order, slug in enumerate([*listed, *(c.slug for c in others)]):
                shelf_def = next((s for s in shelves if s[0] == slug), None)
                shelf = ensure(*shelf_def) if shelf_def else cats[slug]
                if shelf.parent_id != group.id or shelf.sort_order != s_order:
                    report["moved"].append({"slug": slug, "parent": g_slug, "sort_order": s_order})
                shelf.parent_id, shelf.sort_order = group.id, s_order
                if slug in RENAMED and shelf_def and shelf.name != shelf_def[1]:
                    report["renamed"].append({"slug": slug, "from": shelf.name, "to": shelf_def[1]})
                    shelf.name = shelf_def[1]
                    shelf.translations = with_names(shelf, shelf_def[2], shelf_def[3])
                # `is not False`: a shelf created just now has no value until it is written.
                if slug in SWITCHED_OFF and shelf.is_active is not False:
                    shelf.is_active = False
                    report["switched_off"].append(slug)
        await db.flush()

        by_id = {c.id: c for c in cats.values()}
        parent_slug = {c.slug: (by_id[c.parent_id].slug if c.parent_id else None) for c in cats.values()}

        def ancestors(slug: str) -> list[str]:
            up = parent_slug.get(slug)
            return [up, *ancestors(up)] if up else []

        def depth(slug: str) -> int:
            return 1 + len(ancestors(slug))

        def height(category: Category) -> int:
            return 1 + max((height(c) for c in cats.values() if c.parent_id == category.id), default=0)

        too_deep = [s for s in cats if depth(s) > MAX_CATEGORY_DEPTH]
        if depth(ROOT[0]) + height(root) - 1 > MAX_CATEGORY_DEPTH or too_deep:
            raise SystemExit(f"Refusing: the menu would be deeper than {MAX_CATEGORY_DEPTH} levels ({', '.join(too_deep)})")

        # 2. Products, by name. Plain rows for the links: they are deleted and
        # written again below, and a loaded one would clash with its rewritten self.
        links = (await db.execute(select(ProductCategory.product_id, ProductCategory.category_id, ProductCategory.position))).all()
        listed_on: dict = {}
        for product_id, category_id, _ in sorted(links, key=lambda l: l.position):
            listed_on.setdefault(product_id, []).append(by_id[category_id].slug)
        products = (await db.execute(select(Product).order_by(Product.name))).scalars().all()
        tally = Counter()
        for product in products:
            before = listed_on.get(product.id, [])
            primary = by_id[product.category_id].slug if product.category_id in by_id else None
            if primary and primary not in before:
                before = [primary, *before]
            if is_set(product.name, set(before)):
                tally["set"] += 1
                continue
            # As in October: a herbal product stays on its shelf, whatever its name says.
            kind = "bitkisel-urunler" if "bitkisel-urunler" in before else kind_of(product.name)
            # Its listings on other shelves (a brand's own page, Fırsat Ürünler,
            # Nargile Malzemeleri) stay: only a wrong shelf of this tree goes.
            kept = [s for s in before if s not in LEAVES and s not in MERGED]
            if kind in LEAVES:
                after = [kind, *kept]
            elif any(s in MERGED for s in before):
                # On Pürmüzler with no word for it: Gazlar takes it, in Pürmüzler's place.
                after = list(dict.fromkeys(MERGED.get(s, s) for s in before))
                kind = "merged"
            elif kind is None and set(before) & SET_SHELVES:
                # On a set shelf with no word for a part: a set sold under its line's name.
                tally["set"] += 1
                continue
            else:
                tally["unmatched" if kind is None else "other"] += 1
                if kind is None:
                    report["unmatched"].append({"id": str(product.id), "name": product.name, "shelves": before})
                continue
            if after == before and primary == after[0]:
                tally["in_place"] += 1
                continue
            tally["moved"] += 1
            report["products"].append({"id": str(product.id), "name": product.name, "before": before, "after": after})
            await db.execute(delete(ProductCategory).where(ProductCategory.product_id == product.id))
            for position, slug in enumerate(after):
                db.add(ProductCategory(product_id=product.id, category_id=cats[slug].id, position=position))
            product.category_id = cats[after[0]].id
            # Its hand-set place on a page it is no longer on (a group still lists its shelves').
            still = set(after) | {up for s in after for up in ancestors(s)}
            gone = [cats[s].id for s in set(before) - still if s in cats]
            if gone:
                await db.execute(
                    delete(CategoryProductOrder).where(
                        CategoryProductOrder.product_id == product.id, CategoryProductOrder.category_id.in_(gone)
                    )
                )
        await db.flush()

        # 3. The merged shelf, now empty, goes.
        for slug in MERGED:
            category = cats.get(slug)
            if category is None:
                continue
            listed_here = await db.scalar(select(ProductCategory.product_id).where(ProductCategory.category_id == category.id).limit(1))
            primary_here = await db.scalar(select(Product.id).where(Product.category_id == category.id).limit(1))
            children = await db.scalar(select(Category.id).where(Category.parent_id == category.id).limit(1))
            if listed_here or primary_here or children:
                raise SystemExit(f"Refusing to delete {slug}: it still has products or shelves")
            await db.execute(delete(CategoryProductOrder).where(CategoryProductOrder.category_id == category.id))
            await db.delete(category)
            await db.flush()
            report["deleted"].append(slug)

        # 4. What each shelf of the tree holds now, and what this run put there.
        moved_to = Counter(row["after"][0] for row in report["products"])
        # Listed by a link or by the primary alone (rows from before the links), each product once.
        members = set((await db.execute(select(ProductCategory.category_id, ProductCategory.product_id))).all())
        members |= set((await db.execute(select(Product.category_id, Product.id).where(Product.category_id.is_not(None)))).all())
        on_shelf = Counter(by_id[c].slug for c, _ in members if c in by_id)
        report["shelves"] = []
        print(f"categories created: {report['created'] or '-'}; moved/reordered: {len(report['moved'])}; "
              f"renamed: {[r['to'] for r in report['renamed']] or '-'}; switched off: {report['switched_off'] or '-'}; "
              f"deleted: {report['deleted'] or '-'}")
        print(f"products: {len(products)} — moved {tally['moved']}, already in place {tally['in_place']}, "
              f"sets left alone {tally['set']}, another shelf's kind (left alone) {tally['other']}, unmatched {tally['unmatched']}")
        print("\nshelf                              moved now   on the shelf")
        for g_slug, g_name, *_, shelves in TREE:
            print(f"{cats[g_slug].name}")
            for slug, *_ in shelves:
                name = cats[slug].name + ("" if cats[slug].is_active else " (pasif)")
                report["shelves"].append({"slug": slug, "moved": moved_to[slug], "total": on_shelf[slug]})
                print(f"  {name:<34}{moved_to[slug]:>6}{on_shelf[slug]:>14}")
        print(f"\nmoved ({len(report['products'])}):")
        for row in report["products"]:
            print(f"  {row['name']}  [{', '.join(row['before']) or 'kategorisiz'}] -> [{', '.join(row['after'])}]")
        print(f"\nunmatched ({len(report['unmatched'])}) — no rule knows the name; left where they are:")
        for row in report["unmatched"]:
            print(f"  {row['name']}  [{', '.join(row['shelves']) or 'kategorisiz'}]")

        if dry_run:
            await db.rollback()
            print("\ndry run: nothing written")
            return
        await db.commit()
        out = Path(__file__).resolve().parent / "migration_reports" / f"malzemeler-tree-{datetime.now():%Y%m%d-%H%M%S}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"\nwritten; report: {out}")
        if refresh:
            await refresh_storefront()
        else:
            print("storefront cache: not refreshed (--refresh-storefront asks for it; or an admin save)")


async def refresh_storefront() -> None:
    """The storefront's cached catalog dropped, so the new tree shows on the next request."""
    if not (settings.STOREFRONT_REVALIDATE_URL and settings.REVALIDATE_SECRET):
        print("storefront cache: not configured here — refresh it with an admin save")
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(settings.STOREFRONT_REVALIDATE_URL, headers={"X-Revalidate-Secret": settings.REVALIDATE_SECRET})
        print(f"storefront cache refreshed: HTTP {resp.status_code}")
    except httpx.HTTPError as exc:
        print(f"storefront cache: refresh failed ({exc}) — refresh it with an admin save")


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
