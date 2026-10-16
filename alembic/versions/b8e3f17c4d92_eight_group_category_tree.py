"""Category tree: eight main groups and their subcategories

Revision ID: b8e3f17c4d92
Revises: e1f7c3a90b24
Create Date: 2026-10-14

Replaces the tree that grew after e7c4b19a2f60 (brand roots such as "Panada
Hookah" with "Sultan Nargile", "Legend Hookah"… under them) with the shop's
eight main groups. Brands are a product attribute, not categories: the brand
entries in the menus point at the brand list, and stay.

Nothing is lost on the way, as in e7c4b19a2f60:
- a category that already has the right slug, or whose old slug is listed in
  RENAME, is updated in place, keeping its id, its cover image and its
  products ("nargile-komurleri" keeps its slug, so the coal rules still apply);
- every other old category's products move to the category in MOVE, then the
  ones sitting directly on a group are sorted into a subcategory by name
  (REFINE) where the name says which one it is; the rest stay on the group,
  whose page lists them all the same;
- links to old category pages (homepage menus and showcase, footer, hero
  slides, promo boxes) are rewritten to the new pages. The sidebar's category
  entries are replaced with the eight groups; its brand entries stay.

Only then are the old categories removed. Downgrade is not supported: restore
backups/before-category-tree-*.dump.
"""
import json
import re
import unicodedata
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8e3f17c4d92"
down_revision: Union[str, None] = "e1f7c3a90b24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (slug, name, menu label, [(child slug, child name), …]) — in menu order.
TAXONOMY = [
    ("nargile-takimlari", "Nargile Takımları", "NARGİLE TAKIMLARI", [
        ("cantali-nargile-takimlari", "Çantalı Nargile Takımları"),
        ("kafe-tipi-nargile-takimlari", "Kafe Tipi Nargile Takımları"),
        ("masaustu-nargile-takimlari", "Masaüstü Nargile Takımları"),
    ]),
    ("luleler", "Lüleler", "LÜLELER", [
        ("killer", "Killer"),
        ("phunnel", "Phunnel"),
        ("cam-ve-silikon", "Cam ve Silikon"),
    ]),
    ("sarf-malzemeleri", "Sarf Malzemeleri", "SARF MALZEMELERİ", [
        ("nargile-komurleri", "Nargile Kömürleri"),
        ("bitkisel-urunler", "Bitkisel Ürünler"),
        ("folyolar", "Folyolar"),
        ("sipsiler", "Sipsiler"),
        ("kullan-at-marpuclar", "Kullan At Marpuçlar"),
    ]),
    ("aksesuarlar", "Aksesuarlar", "AKSESUARLAR", [
        ("askili-sipsiler", "Askılı Sipsiler"),
        ("tutun-catallari", "Tütün Çatalları"),
        ("nargile-matlari", "Nargile Matları"),
        ("serbetlikler", "Şerbetlikler"),
        ("cantalar", "Çantalar"),
        ("elekler", "Elekler"),
        ("deliciler", "Deliciler"),
        ("koz-tavalari", "Köz Tavaları"),
        ("bacalar", "Bacalar"),
        ("ruzgarliklar", "Rüzgarlıklar"),
        ("masalar", "Maşalar"),
    ]),
    ("marpuclar", "Marpuçlar", "MARPUÇLAR", [
        ("silikon-marpuclar", "Silikon Marpuçlar"),
        ("silikon-hortumlar", "Silikon Hortumlar"),
        ("deri-marpuclar", "Deri Marpuçlar"),
        ("marpuc-basliklari", "Marpuç Başlıkları"),
    ]),
    ("isi-yonetim-araclari", "Isı Yönetim Araçları", "ISI YÖNETİM ARAÇLARI", [
        ("kozlukler", "Közlükler"),
    ]),
    ("yedek-parca", "Yedek Parça", "YEDEK PARÇA", [
        ("nargile-siseleri", "Nargile Şişeleri"),
        ("rezistans-izgara", "Rezistans & Izgara"),
        ("adaptorler-difuzorler", "Adaptörler & Difüzörler"),
        ("yapistiricilar", "Yapıştırıcılar"),
        ("bilyalar", "Bilyalar"),
        ("contalar-bez-bantlar", "Contalar & Bez Bantlar"),
    ]),
    ("yakicilar", "Yakıcılar", "YAKICILAR", [
        ("elektrikli-ocaklar", "Elektrikli Ocaklar"),
        ("purmuzler", "Pürmüzler"),
        ("cakmaklar", "Çakmaklar"),
        ("gazlar", "Gazlar"),
    ]),
]

# Old slug → new slug for a category that is the same one under a new name: the
# row is kept (id, image, products) and renamed.
RENAME = {
    "phunnel-luleler": "phunnel",
    "killer-luleler": "killer",
    "cam-silikon-luleler": "cam-ve-silikon",
    "mini-masaustu-takimlar": "masaustu-nargile-takimlari",
    "yedek-sise-ve-govde": "nargile-siseleri",
    "isi-yonetimi": "isi-yonetim-araclari",
    "marpuc-ve-hortumlar": "marpuclar",
    "sipsiler-ve-aksesuarlar": "aksesuarlar",
    "canta-ve-tasima": "cantalar",
    "bitkisel-karisimlar": "bitkisel-urunler",
}

# Where the products and links of each category this migration removes go.
MOVE = {
    # The brand roots and the takım types: all nargile sets.
    "panada-hookah": "nargile-takimlari",
    "sultan-nargile": "nargile-takimlari",
    "legend-hookah": "nargile-takimlari",
    "oduman-nargile": "nargile-takimlari",
    "mr-wood-nargile": "nargile-takimlari",
    "steamulation-nargile-takimlari": "nargile-takimlari",
    "quasar-arguile": "nargile-takimlari",
    "meduse-nargile-takimlari": "nargile-takimlari",
    "klasik-takimlar": "nargile-takimlari",
    "celik-takimlar": "nargile-takimlari",
    "rus-takimlari": "nargile-takimlari",
    "conta-ve-yedek-parca": "yedek-parca",
    "temizlik-ve-bakim": "aksesuarlar",
    "hindistan-cevizi-komurleri": "nargile-komurleri",
    "tutun-bitkisel-ve-sarf": "sarf-malzemeleri",
    # Only ever in the storefront's built-in menus, never a row: links only.
    "kozlukler-isi-yonetimi": "isi-yonetim-araclari",
}

# A product sitting directly on a group goes to the first subcategory whose
# words its name contains (compared without Turkish letters, upper case).
REFINE = {
    "nargile-takimlari": [
        ("MARPUC BASLIGI", "marpuc-basliklari"),
        ("CANTALI", "cantali-nargile-takimlari"),
    ],
    "luleler": [
        ("KILLER", "killer"),
        ("PHUNNEL", "phunnel"),
        ("CAM", "cam-ve-silikon"),
        ("SILIKON", "cam-ve-silikon"),
    ],
    "aksesuarlar": [
        ("BACA", "bacalar"),
        ("BEZ BANT", "contalar-bez-bantlar"),
        ("CONTA", "contalar-bez-bantlar"),
        ("DIFUZOR", "adaptorler-difuzorler"),
        ("ADAPTOR", "adaptorler-difuzorler"),
        ("PURMUZ GAZI", "gazlar"),
        ("LIGHTER FLUID", "gazlar"),
        ("CAKMAK GAZI", "gazlar"),
    ],
}

NEW_SLUGS = {slug for slug, _, _, kids in TAXONOMY for slug in [slug, *(k for k, _ in kids)]}
GROUP_SLUGS = {slug for slug, _, _, _ in TAXONOMY}
CATALOG_LINK = re.compile(r"^/catalog/([a-z0-9-]+)(/?(?:[?#].*)?)$")


def _words(name: str) -> str:
    """'ÇANTALI çelik' → ' CANTALI CELIK ': ASCII, upper case, padded for whole-word matches."""
    name = name.replace("ı", "i").replace("İ", "I")
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return " " + re.sub(r"[^A-Z0-9]+", " ", ascii_name.upper()) + " "


def upgrade() -> None:
    conn = op.get_bind()
    old = conn.execute(sa.text("SELECT id, slug FROM categories")).mappings().all()
    present = {row["slug"] for row in old}

    # Where every old slug's products and links end up.
    target = {**{s: s for s in NEW_SLUGS}, **RENAME, **MOVE}

    # 1. Flatten first, so re-parenting never briefly nests three deep; then
    #    renames take their new slug (the new slug cannot be taken yet: none of
    #    them existed before this migration).
    conn.execute(sa.text("UPDATE categories SET parent_id = NULL"))
    for old_slug, new_slug in RENAME.items():
        if old_slug in present and new_slug not in present:
            conn.execute(sa.text("UPDATE categories SET slug = :n WHERE slug = :o"), {"n": new_slug, "o": old_slug})

    # 2. The new tree: update a row that already has the slug, else insert.
    upsert = sa.text(
        "INSERT INTO categories (name, slug, parent_id, sort_order, is_active) "
        "VALUES (:name, :slug, :parent, :sort, true) "
        "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, parent_id = EXCLUDED.parent_id, "
        "sort_order = EXCLUDED.sort_order, is_active = true"
    )
    ids: dict[str, int] = {}

    def put(slug: str, name: str, parent: int | None, sort: int) -> None:
        conn.execute(upsert, {"name": name, "slug": slug, "parent": parent, "sort": sort})
        ids[slug] = conn.execute(sa.text("SELECT id FROM categories WHERE slug = :s"), {"s": slug}).scalar_one()

    for i, (slug, name, _, kids) in enumerate(TAXONOMY, start=1):
        put(slug, name, None, i)
        for j, (kid, kid_name) in enumerate(kids, start=1):
            put(kid, kid_name, ids[slug], j)

    # 3. Products of removed categories follow MOVE (unknown ones: uncategorised).
    stale = [r for r in old if r["slug"] not in NEW_SLUGS and r["slug"] not in RENAME]
    for row in stale:
        new = MOVE.get(row["slug"])
        conn.execute(
            sa.text("UPDATE products SET category_id = :new WHERE category_id = :old"),
            {"new": ids[new] if new else None, "old": row["id"]},
        )

    # 4. …then into a subcategory where their name says which.
    for group, rules in REFINE.items():
        rows = conn.execute(sa.text("SELECT id, name FROM products WHERE category_id = :g"), {"g": ids[group]}).all()
        for pid, name in rows:
            words = _words(name)
            kid = next((kid for word, kid in rules if f" {word} " in words), None)
            if kid:
                conn.execute(sa.text("UPDATE products SET category_id = :c WHERE id = :p"), {"c": ids[kid], "p": pid})

    # 5. Links to old category pages, wherever the editor stores them.
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

    def is_category(item: dict) -> bool:
        return bool(item.get("slug")) or bool(item.get("href") and CATALOG_LINK.match(item["href"]))

    def top_bar(items: list[dict]) -> list[dict]:
        """Relinked, without a second entry for the same page (two old ones may merge)."""
        seen: set[str] = set()
        out = []
        for item in map(reslug, items):
            key = item.get("slug") or item.get("href") or ""
            if key and key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def sidebar(items: list[dict]) -> list[dict]:
        """The category entries give way to the eight groups, where the first of them was."""
        groups = [
            {"label": label, "slug": slug, "accent": False, "enabled": True} for slug, _, label, _ in TAXONOMY
        ]
        at = next((i for i, item in enumerate(items) if is_category(item)), len(items))
        rest = [item for item in items if not is_category(item)]
        before = [item for item in items[:at] if not is_category(item)]
        return before + groups + rest[len(before):]

    row = conn.execute(sa.text("SELECT data FROM site_content WHERE key = 'homepage'")).scalar_one_or_none()
    if row:
        data = row if isinstance(row, dict) else json.loads(row)
        nav = data.get("navigation")
        if nav:
            if "top" in nav:
                nav["top"] = top_bar(nav["top"])
            if "sidebar" in nav:
                nav["sidebar"] = sidebar(nav["sidebar"])
            data["navigation"] = nav
        if data.get("showcase"):
            data["showcase"] = {**data["showcase"], "tiles": [reslug(t) for t in data["showcase"].get("tiles", [])]}
        footer = data.get("footer")
        if footer:
            for col in ("blog", "info", "faq"):
                if footer.get(col):
                    footer[col] = {**footer[col], "links": [{**l, "href": relink(l["href"])} for l in footer[col]["links"]]}
        conn.execute(sa.text("UPDATE site_content SET data = CAST(:d AS JSONB) WHERE key = 'homepage'"), {"d": json.dumps(data)})

    # 6. The old tree goes (already flattened in step 1, and emptied in step 3).
    for r in stale:
        conn.execute(sa.text("DELETE FROM categories WHERE id = :i"), {"i": r["id"]})


def downgrade() -> None:
    raise NotImplementedError("The old category tree cannot be rebuilt; restore backups/before-category-tree-*.dump.")
