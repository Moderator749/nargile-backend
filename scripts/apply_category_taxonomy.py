"""The shop's category tree of 2026-10-29 ("katagori.pdf"): seven groups, each
product on the one shelf its kind belongs on.

- Nargile Takımları holds complete sets only, on their brand's and their
  kind's shelves as before; a part sold by a set's brand (a Steamulation hose
  handle, a Quasar bowl) leaves them for the shelf of its kind.
- Every other product is placed by what it is (RULES, first match wins),
  never by its brand: a Sultan glass base is a glass base (Şişeler).
- Shelves that carry on keep their slug, name and address (/catalog/siseler),
  and only move under their group; the storefront's menus name them by slug,
  so they look as before. New groups take the slugs the storefront already
  has copy for (lib/categories): sarf-malzemeleri, aksesuarlar, yedek-parca,
  yakicilar.
- "Nargile Malzemeleri" (malzemeler) stays outside the tree, as the top
  menu's page: its products keep it as a second listing. Fırsat Ürünler
  (kampanyalar) is the shop's hand-picked list and is kept as listed.
- The test shelves "ayakkabı" and "çorap" (empty) are deleted.

    .venv/bin/python scripts/apply_category_taxonomy.py --dry-run   # show, write nothing
    .venv/bin/python scripts/apply_category_taxonomy.py             # write (pg_dump first)

Every run writes what it changed, product by product, to
scripts/migration_reports/taxonomy-<date>.json. All of it is one transaction;
the way back is the dump taken before. Afterwards the storefront's catalog
cache needs refreshing (an admin save, or POST /api/revalidate).
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

from sqlalchemy import delete, select  # noqa: E402

from app.core.database import async_session, engine  # noqa: E402
from app.models.catalog import Category, CategoryProductOrder, Product, ProductCategory  # noqa: E402

# slug, Turkish name, English, Arabic — the name is only written for a shelf
# that does not exist yet; one that does keeps its own.
GROUPS = [
    ("kampanyalar", "Fırsat Ürünler", None, None, []),
    ("nargile-takimlari", "Nargile Takımları", None, None, [
        ("sultan", "Sultan Nargile"), ("legend", "Legend Hookah"), ("oduman", "Oduman Nargile"),
        ("mr-wood", "Mr.Wood Nargile Takımları"), ("klasik-takimlar", "Klasik Nargile Takımları"),
        ("cantali-nargile-takimlari", "Çantalı Nargile Takımları"), ("celik-takimlar", "Çelik Nargile Takımları"),
        ("masaustu-takimlar", "Masaüstü Nargile Takımları"), ("rus-takimlar", "Rus Nargile Takımları"),
        ("steamulation", "Steamulation Nargile Takımları"), ("quasar", "Quasar Arguile"),
        ("meduse", "Meduse Nargile Takımları"), ("panada", "Panada Hookah"),
    ]),
    ("sarf-malzemeleri", "Sarf Tüketim Malzemeleri", "Consumables", "مستلزمات استهلاكية", [
        ("komurler", "Kömürler"), ("bitkisel-urunler", "Bitkisel Ürünler"),
        ("folyolar", "Folyolar", "Hookah Foil", "ورق ألمنيوم للشيشة"),
        ("sipsiler", "Sipsiler"),
        ("kullan-at-marpuclar", "Kullan At Marpuçlar", "Disposable Hoses", "لي شيشة للاستخدام مرة واحدة"),
        ("bez-bantlar", "Bez Bantlar", "Cloth Tapes", "أشرطة قماشية"),
    ]),
    ("aksesuarlar", "Aksesuarlar", "Accessories", "إكسسوارات", [
        ("nargile-cantalari", "Nargile Çantaları", "Hookah Bags", "حقائب شيشة"),
        ("koz-tavalari-ve-bacalari", "Köz Tavaları ve Bacaları", "Charcoal Pans and Chimneys", "صحون ومداخن الفحم"),
        ("koz-masalari", "Köz Maşaları", "Charcoal Tongs", "ملاقط الفحم"),
        ("ruzgarliklar", "Rüzgarlıklar", "Wind Covers", "أغطية الرياح"),
        ("temizlik-fircalari", "Temizlik Fırçaları", "Cleaning Brushes", "فرش تنظيف"),
        ("luleler", "Lüleler"),
        ("ozel-sipsiler", "Özel Sipsiler", "Premium Mouthpieces", "مباسم فاخرة"),
        ("led-isiklar", "Led Işıklar", "LED Lights", "إضاءة LED"),
        ("deliciler", "Deliciler", "Foil Pokers", "أدوات ثقب الورق"),
        ("elekler", "Elekler", "Screens", "شبكات"),
        ("catallar", "Çatallar", "Hookah Forks", "شوك الشيشة"),
        ("lule-tipalari", "Lüle Tıpaları", "Bowl Plugs", "سدادات الرأس"),
        ("pompalar", "Pompalar", "Pumps", "مضخات"),
        # The shop's word: heat management devices are közlükler too.
        ("kozlukler", "Közlükler"),
        ("marpuclar", "Marpuçlar"),
    ]),
    ("yedek-parca", "Yedek Parçalar", "Spare Parts", "قطع غيار", [
        ("siseler", "Şişeler"),
        ("izgaralar", "Izgaralar", "Grates", "شبكات الموقد"),
        ("rezistanslar", "Rezistanslar", "Heating Elements", "مقاومات تسخين"),
        ("contalar", "Contalar", "Grommets", "جلدات"),
        ("susturucular", "Susturucular", "Diffusers", "ناشرات"),
        ("lule-ve-marpuc-baglantilari", "Lüle ve Marpuç Bağlantıları", "Bowl and Hose Connectors", "وصلات الرأس واللي"),
        ("yapistiricilar", "Yapıştırıcılar", "Adhesives", "مواد لاصقة"),
        ("brulorler", "Brülörler", "Burner Heads", "رؤوس المواقد"),
        ("hortum-yaylari", "Hortum Yayları", "Hose Springs", "نوابض اللي"),
    ]),
    ("oyun-eglence", "Oyun & Eğlence", None, None, [
        ("tavlalar", "Tavlalar", "Backgammon Sets", "طاولات زهر"),
        ("okey-takimlari", "Okey Takımları", "Okey Sets", "أطقم أوكي"),
        ("iskambil-kartlari", "İskambil Kartları", "Playing Cards", "أوراق اللعب"),
    ]),
    ("yakicilar", "Yakıcılar", "Burners and Lighters", "مواقد وولاعات", [
        ("elektrikli-ocaklar", "Elektrikli Ocaklar", "Electric Burners", "مواقد كهربائية"),
        ("gazli-ocaklar", "Gazlı Ocaklar", "Gas Burners", "مواقد غاز"),
        ("gazlar", "Gazlar", "Lighter Gas", "غاز الولاعات"),
        ("purmuzler", "Pürmüzler", "Torches", "ولاعات شعلة"),
        ("cakmaklar", "Çakmaklar", "Lighters", "ولاعات"),
    ]),
]
# Kept as they are, outside the tree: the top menu's page, and the shop's own picks.
OUTSIDE = ("malzemeler",)
KEPT_LISTINGS = ("kampanyalar", "malzemeler")
DELETED = ("corap", "ayakkabi")  # child first

SET_SHELVES = {"nargile-takimlari"} | {s[0] for s in GROUPS[1][4]}


def low(text: str) -> str:
    return text.replace("İ", "i").replace("I", "ı").lower()


# Sets sold under their line's name, not "takımı" (Mr.Wood Artemis: confirmed a set).
SET_LINES = ("meduse experience", "alpha hookah model", "mattpear mini slim", "mr.wood classic artemis", "wd hookah ")


def is_set(name: str, shelves: set[str]) -> bool:
    n = low(name)
    if n.strip() == "quasar arguile" or any(line in n for line in SET_LINES):
        return True
    if re.search(r"okey|tavla", n):  # "Okey Takımı" is a game
        return False
    return bool(re.search(r"nargile takım|takımı$|takımı -", n)) or (bool(shelves & SET_SHELVES) and "takım" in n)


# First match wins. None: a sipsi, split by kind below.
RULES = [
    (r"kömür", "komurler"),
    (r"delici", "deliciler"),
    (r"folyo", "folyolar"),
    (r"kullan at", "kullan-at-marpuclar"),
    (r"bez bant", "bez-bantlar"),
    (r"sipsi", None),
    (r"nargile çantası", "nargile-cantalari"),
    (r"ızgara", "izgaralar"),
    (r"brülör", "brulorler"),
    (r"köz tavası|holder|^baca", "koz-tavalari-ve-bacalari"),
    (r"maşa", "koz-masalari"),
    (r"rüzgarlı", "ruzgarliklar"),
    (r"harbi|fırça|temizleme", "temizlik-fircalari"),
    (r"pompa", "pompalar"),
    (r"çatal", "catallar"),
    (r"lüle tıpası", "lule-tipalari"),
    (r"^led\b|şişe altı led|tepsi altı led", "led-isiklar"),
    (r"rezistans", "rezistanslar"),
    (r"conta", "contalar"),
    (r"difüzör|susturucu", "susturucular"),
    (r"yapıştırıcı|epoksi", "yapistiricilar"),
    (r"marpuç yayı|hortum yayı", "hortum-yaylari"),
    (r"adaptör|ser girişi|marpuç girişi|bağlantı|tahliye", "lule-ve-marpuc-baglantilari"),
    (r"elektrikli ocak", "elektrikli-ocaklar"),
    (r"gazlı ocak|nargile ocağı", "gazli-ocaklar"),
    (r"gazı|lighter gas|fluid|fuel|gaz kartuşu|universal gaz", "gazlar"),
    (r"pürmüz", "purmuzler"),
    (r"çakmak", "cakmaklar"),
    (r"tavla", "tavlalar"),
    (r"okey", "okey-takimlari"),
    (r"iskambil", "iskambil-kartlari"),
    (r"lüle üstü elek|lüle içi elek|süzgeç", "elekler"),
    (r"közlük|cezve|guard|lotus|copper balance|hms|kolodka|elekli lüle başlığı", "kozlukler"),
    (r"marpuç|marpucu|hortumu", "marpuclar"),
    (r"lüle|bowl|phunnel", "luleler"),
    (r"şişe", "siseler"),
]
# Kinds with no shelf of their own in the tree (a set's stand or base, mats,
# molasses catchers): on the Aksesuarlar group itself.
TO_GROUP = [(r"^quasar (led base|tripod ayak)$|nargile matı|şerbetlik", "aksesuarlar")]


def shelf_for(name: str, shelves: set[str], leaves: set[str]) -> str | None:
    n = low(name)
    if "bitkisel-urunler" in shelves:
        return "bitkisel-urunler"
    for pattern, slug in TO_GROUP:
        if re.search(pattern, n.strip()):
            return slug
    for pattern, slug in RULES:
        if re.search(pattern, n):
            if slug is None:
                return "ozel-sipsiler" if re.search(r"askılı|kehribar|çelik|boyun|werkbund|d-stick", n) else "sipsiler"
            return slug
    # No word to go by: the shelf it is on, if that one carries on.
    return next((s for s in sorted(shelves) if s in leaves and s not in SET_SHELVES), None)


async def run(dry_run: bool) -> None:
    async with async_session() as db:
        cats = {c.slug: c for c in (await db.execute(select(Category))).scalars()}
        report: dict = {"created": [], "moved": [], "deleted": [], "products": [], "unplaced": []}

        # 1. The tree: groups, then their shelves, in the PDF's order.
        leaves: set[str] = set()
        for g_order, (g_slug, g_name, g_en, g_ar, shelves) in enumerate(GROUPS):
            group = cats.get(g_slug)
            if group is None:
                group = Category(name=g_name, slug=g_slug, translations={"en": {"name": g_en}, "ar": {"name": g_ar}})
                db.add(group)
                cats[g_slug] = group
                report["created"].append(g_slug)
            if group.parent_id is not None or group.sort_order != g_order:
                report["moved"].append({"slug": g_slug, "parent": None, "sort_order": g_order})
            group.parent_id, group.sort_order = None, g_order
            await db.flush()
            for s_order, shelf_def in enumerate(shelves):
                slug, name = shelf_def[0], shelf_def[1]
                shelf = cats.get(slug)
                if shelf is None:
                    en, ar = shelf_def[2], shelf_def[3]
                    shelf = Category(name=name, slug=slug, translations={"en": {"name": en}, "ar": {"name": ar}})
                    db.add(shelf)
                    cats[slug] = shelf
                    report["created"].append(slug)
                if shelf.parent_id != group.id or shelf.sort_order != s_order:
                    report["moved"].append({"slug": slug, "parent": g_slug, "sort_order": s_order})
                shelf.parent_id, shelf.sort_order = group.id, s_order
                leaves.add(slug)
        for o_order, slug in enumerate(OUTSIDE, start=len(GROUPS)):
            cats[slug].parent_id, cats[slug].sort_order = None, o_order
        await db.flush()
        by_id = {c.id: c for c in cats.values()}
        parent_slug = {c.slug: (by_id[c.parent_id].slug if c.parent_id else None) for c in cats.values()}

        # 2. Every product (published or not) onto its shelves.
        # As plain rows: the links are deleted and written again below, and a
        # loaded one would clash with its rewritten self in the session.
        links = (await db.execute(select(ProductCategory.product_id, ProductCategory.category_id, ProductCategory.position))).all()
        listed: dict = {}
        for product_id, category_id, _ in sorted(links, key=lambda l: l.position):
            listed.setdefault(product_id, []).append(by_id[category_id].slug)
        products = (await db.execute(select(Product))).scalars().all()
        for product in products:
            before = listed.get(product.id, [])
            primary = by_id[product.category_id].slug if product.category_id in by_id else None
            if primary and primary not in before:
                before = [primary, *before]
            shelves = set(before)
            kept = [s for s in before if s in KEPT_LISTINGS]
            if is_set(product.name, shelves):
                after = [s for s in before if s in SET_SHELVES] or before
                after = after + [s for s in kept if s not in after]
            else:
                shelf = shelf_for(product.name, shelves, leaves)
                if shelf is None:
                    # Left where it is, unless that is a set shelf: those hold sets only.
                    if shelves & SET_SHELVES:
                        raise SystemExit(f"Refusing: {product.name} is not a set and has no shelf to go to")
                    report["unplaced"].append({"id": str(product.id), "name": product.name, "shelves": before})
                    continue
                after = [shelf, *[s for s in kept if s != shelf]]
            if after == before and primary == after[0]:
                continue
            report["products"].append({"id": str(product.id), "name": product.name, "before": before, "after": after})
            await db.execute(delete(ProductCategory).where(ProductCategory.product_id == product.id))
            for position, slug in enumerate(after):
                db.add(ProductCategory(product_id=product.id, category_id=cats[slug].id, position=position))
            product.category_id = cats[after[0]].id
            # Its hand-set place on a page it is no longer on (a group still
            # lists what its shelves do).
            still = {s for s in after} | {parent_slug[s] for s in after if parent_slug.get(s)}
            gone = [cats[s].id for s in shelves - still if s in cats]
            if gone:
                await db.execute(
                    delete(CategoryProductOrder).where(
                        CategoryProductOrder.product_id == product.id, CategoryProductOrder.category_id.in_(gone)
                    )
                )

        # 3. Guard: no set shelf holds anything but a set.
        await db.flush()
        for row in report["products"]:
            if set(row["after"]) & SET_SHELVES and not is_set(row["name"], set(row["before"])):
                raise SystemExit(f"Refusing: {row['name']} would stay on a set shelf")

        # 4. The empty test shelves.
        for slug in DELETED:
            category = cats.get(slug)
            if category is None:
                continue
            listed_here = await db.scalar(select(ProductCategory.product_id).where(ProductCategory.category_id == category.id).limit(1))
            primary_here = await db.scalar(select(Product.id).where(Product.category_id == category.id).limit(1))
            if listed_here or primary_here:
                raise SystemExit(f"Refusing to delete {slug}: it has products")
            await db.delete(category)
            await db.flush()
            report["deleted"].append(slug)

        moves = Counter((tuple(r["before"]), tuple(r["after"])) for r in report["products"])
        print(f"categories created: {len(report['created'])}, moved/reordered: {len(report['moved'])}, deleted: {report['deleted']}")
        print(f"products changed: {len(report['products'])} of {len(products)}; unplaced (left as they were): {len(report['unplaced'])}")
        for (before, after), n in moves.most_common():
            print(f"  {n:4d}  {', '.join(before) or '-'}  ->  {', '.join(after)}")
        for row in report["unplaced"]:
            print(f"  unplaced: {row['name']} ({', '.join(row['shelves']) or 'no shelf'})")

        if dry_run:
            await db.rollback()
            print("dry run: nothing written")
            return
        await db.commit()
        out = Path(__file__).resolve().parent / "migration_reports" / f"taxonomy-{datetime.now():%Y%m%d-%H%M%S}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"written; report: {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    args = parser.parse_args()

    async def go():
        try:
            await run(args.dry_run)
        finally:
            await engine.dispose()

    asyncio.run(go())


if __name__ == "__main__":
    main()
