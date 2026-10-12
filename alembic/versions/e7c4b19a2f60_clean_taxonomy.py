"""Clean category taxonomy: seven groups, designed from scratch

Revision ID: e7c4b19a2f60
Revises: d2a9c7f35b18
Create Date: 2026-10-12 18:30:00.000000

Replaces the inherited category tree with one designed for this shop: seven
top-level groups, each with sub-categories that match how the products are
actually chosen (bowl type, heat management, hose material…).

Nothing is lost on the way:
- every product is moved to its new category (a category this migration does
  not know is resolved through its children, else its products are left
  uncategorised rather than guessed);
- links to the old category pages — in the homepage editor's menus and
  showcase, the hero slides and the promo boxes — are rewritten to the new
  pages, so none of them turns into a 404;
- a category that already has the right slug is updated in place, keeping its
  id, its cover image and its products.

Only then are the old categories removed. Downgrade is not supported: the old
tree cannot be rebuilt from the new one (restore the pre-migration backup).
"""
import json
import re
from collections import Counter
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e7c4b19a2f60"
down_revision: Union[str, None] = "d2a9c7f35b18"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (slug, name, [(child slug, child name), …]) — in menu order.
TAXONOMY = [
    ("nargile-takimlari", "Nargile Takımları", [
        ("celik-takimlar", "Çelik Takımlar"),
        ("klasik-takimlar", "Klasik Takımlar"),
        ("rus-takimlari", "Rus Takımları"),
        ("mini-masaustu-takimlar", "Mini & Masaüstü Takımlar"),
        ("yedek-sise-ve-govde", "Yedek Şişe & Gövde"),
    ]),
    ("luleler", "Lüleler", [
        ("phunnel-luleler", "Phunnel Lüleler"),
        ("killer-luleler", "Killer Lüleler"),
        ("sirli-seramik-luleler", "Sırlı & Seramik Lüleler"),
        ("toprak-kil-luleler", "Toprak & Kil Lüleler"),
        ("cam-silikon-luleler", "Cam & Silikon Lüleler"),
    ]),
    ("kozlukler-isi-yonetimi", "Közlükler & Isı Yönetimi", [
        ("kozlukler", "Közlükler (HMD)"),
        ("folyo-ve-ruzgarlik", "Folyo & Rüzgarlık"),
        ("masa-ve-koz-aparatlari", "Maşa & Köz Aparatları"),
    ]),
    ("marpuc-ve-hortumlar", "Marpuç & Hortumlar", [
        ("silikon-marpuclar", "Silikon Marpuçlar"),
        ("klasik-marpuclar", "Klasik & Deri Marpuçlar"),
        ("marpuc-uclari", "Marpuç Uçları & Adaptörler"),
    ]),
    ("sipsiler-ve-aksesuarlar", "Sipsiler & Aksesuarlar", [
        ("sipsiler", "Sipsiler"),
        ("conta-ve-yedek-parca", "Conta & Yedek Parçalar"),
        ("temizlik-ve-bakim", "Temizlik & Bakım"),
        ("canta-ve-tasima", "Çanta & Taşıma"),
    ]),
    ("nargile-komurleri", "Nargile Kömürleri", [
        ("hindistan-cevizi-komurleri", "Hindistan Cevizi Kömürleri"),
        ("hizli-yanan-komurler", "Hızlı Yanan Kömürler"),
        ("komur-ocaklari", "Kömür Ocakları & Kızdırıcılar"),
    ]),
    ("tutun-bitkisel-ve-sarf", "Tütün, Bitkisel Ürünler & Sarf", [
        ("bitkisel-karisimlar", "Bitkisel Karışımlar"),
        ("sarf-malzemeleri", "Sarf Malzemeleri"),
    ]),
]

# Where the products of each category this migration replaces go.
MOVE = {
    "nargile-takimlari": "nargile-takimlari",
    "celik-takimlar": "celik-takimlar",
    "klasik-takimlar": "klasik-takimlar",
    "rus-takimlar": "rus-takimlari",
    "masaustu-takimlar": "mini-masaustu-takimlar",
    "yedek-siseler": "yedek-sise-ve-govde",
    "tuketim-sarf": "tutun-bitkisel-ve-sarf",
    "komurler": "nargile-komurleri",
    "bitkisel-urunler": "bitkisel-karisimlar",
    "sipsiler": "sipsiler",
    "donanim-parcalar": "sipsiler-ve-aksesuarlar",
    "aksesuarlar": "sipsiler-ve-aksesuarlar",
    "luleler": "luleler",
    "kozlukler": "kozlukler",
    "marpuclar": "marpuc-ve-hortumlar",
    "oyun-aletleri": "sipsiler-ve-aksesuarlar",
}

NEW_SLUGS = {slug for slug, _, kids in TAXONOMY for slug in [slug, *(k for k, _ in kids)]}
CATALOG_LINK = re.compile(r"^/catalog/([a-z0-9-]+)(/?(?:[?#].*)?)$")


def upgrade() -> None:
    conn = op.get_bind()
    old = conn.execute(sa.text("SELECT id, slug, parent_id FROM categories")).mappings().all()
    by_id = {row["id"]: row for row in old}

    # 1. Where every existing category's products and links go.
    target: dict[str, str | None] = {}
    for row in old:
        target[row["slug"]] = row["slug"] if row["slug"] in NEW_SLUGS else MOVE.get(row["slug"])
    # A category nobody planned for (renamed by hand, say) follows its children.
    for row in old:
        if target[row["slug"]] is None:
            kids = [target[k["slug"]] for k in old if k["parent_id"] == row["id"] and target[k["slug"]]]
            if kids:
                parents = [p for p, _, ks in TAXONOMY for t in kids if t == p or t in dict(ks)]
                target[row["slug"]] = Counter(parents).most_common(1)[0][0] if parents else None

    # 2. The new tree: update a row that already has the slug, else insert.
    upsert = sa.text(
        "INSERT INTO categories (name, slug, parent_id, sort_order, is_active) "
        "VALUES (:name, :slug, :parent, :sort, true) "
        "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, parent_id = EXCLUDED.parent_id, "
        "sort_order = EXCLUDED.sort_order, is_active = true"
    )
    # Parents first, detached from any old parent, so no row is briefly its own grandparent.
    conn.execute(sa.text("UPDATE categories SET parent_id = NULL WHERE slug = ANY(:s)"), {"s": list(NEW_SLUGS)})
    ids: dict[str, int] = {}
    for i, (slug, name, kids) in enumerate(TAXONOMY):
        conn.execute(upsert, {"name": name, "slug": slug, "parent": None, "sort": (i + 1) * 10})
        ids[slug] = conn.execute(sa.text("SELECT id FROM categories WHERE slug = :s"), {"s": slug}).scalar_one()
    for slug, _, kids in TAXONOMY:
        for j, (kid, kid_name) in enumerate(kids):
            conn.execute(upsert, {"name": kid_name, "slug": kid, "parent": ids[slug], "sort": (j + 1) * 10})
            ids[kid] = conn.execute(sa.text("SELECT id FROM categories WHERE slug = :s"), {"s": kid}).scalar_one()

    # 3. Products follow their category.
    for row in old:
        if row["slug"] in NEW_SLUGS:
            continue
        new = target[row["slug"]]
        conn.execute(
            sa.text("UPDATE products SET category_id = :new WHERE category_id = :old"),
            {"new": ids[new] if new else None, "old": row["id"]},
        )

    # 4. Links to old category pages, wherever the editor stores them.
    def relink(href: str | None) -> str | None:
        if not href:
            return href
        m = CATALOG_LINK.match(href)
        if not m or m.group(1) in NEW_SLUGS:
            return href
        new = target.get(m.group(1))
        return f"/catalog/{new}{m.group(2)}" if new else href

    for table, column in (("hero_slides", "cta_href"), ("hero_slides", "guest_cta_href"), ("promo_boxes", "href")):
        for rid, href in conn.execute(sa.text(f"SELECT id, {column} FROM {table}")).all():
            if (new := relink(href)) != href:
                conn.execute(sa.text(f"UPDATE {table} SET {column} = :h WHERE id = :i"), {"h": new, "i": rid})

    def reslug(item: dict) -> dict:
        slug = item.get("slug")
        if slug and slug not in NEW_SLUGS:
            new = target.get(slug)
            item = {**item, "slug": new} if new else {k: v for k, v in item.items() if k != "slug"}
        if item.get("href"):
            item = {**item, "href": relink(item["href"])}
        return item

    row = conn.execute(sa.text("SELECT data FROM site_content WHERE key = 'homepage'")).scalar_one_or_none()
    if row:
        data = row if isinstance(row, dict) else json.loads(row)
        nav = data.get("navigation")
        if nav:
            data["navigation"] = {**nav, **{k: [reslug(i) for i in nav[k]] for k in ("top", "sidebar") if k in nav}}
        if data.get("showcase"):
            data["showcase"] = {**data["showcase"], "tiles": [reslug(t) for t in data["showcase"].get("tiles", [])]}
        footer = data.get("footer")
        if footer:
            for col in ("blog", "info", "faq"):
                if footer.get(col):
                    footer[col] = {**footer[col], "links": [{**l, "href": relink(l["href"])} for l in footer[col]["links"]]}
        conn.execute(sa.text("UPDATE site_content SET data = CAST(:d AS JSONB) WHERE key = 'homepage'"), {"d": json.dumps(data)})

    # 5. The old tree goes: children before parents.
    stale = [r for r in old if r["slug"] not in NEW_SLUGS]
    for r in sorted(stale, key=lambda r: r["parent_id"] is None):
        conn.execute(sa.text("UPDATE categories SET parent_id = NULL WHERE parent_id = :i"), {"i": r["id"]})
        conn.execute(sa.text("DELETE FROM categories WHERE id = :i"), {"i": r["id"]})


def downgrade() -> None:
    raise NotImplementedError("The old category tree cannot be rebuilt; restore the pre-migration backup.")
