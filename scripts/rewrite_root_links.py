"""Links the shop wrote into its content onto today's addresses: /catalog/<slug>
→ /<slug> and /product/<slug> → /<slug> (2026-10-29, when categories and
products moved to the storefront's root).

The old addresses still answer with a 301 (the storefront's src/proxy.ts), so
nothing is broken before this runs; after it, the shop's own pages link
straight to the page instead of through a redirect. Every text and JSON
column of the content tables is read — the homepage document and its
translations, slides, promo boxes, announcements, the categories', products'
and brands' copy, the legal documents — and only the path is changed, on the
shop's own domain or as a bare path: "/catalog/luleler" and
"https://www.nargilemalzemesi.com/catalog/luleler" alike. A product link by
id (/product/<uuid>) is left to its redirect: the id says nothing of the slug.

    .venv/bin/python scripts/rewrite_root_links.py --dry-run   # show, write nothing
    .venv/bin/python scripts/rewrite_root_links.py             # write (pg_dump first)

Every run writes the values it changes, before and after, to
scripts/migration_reports/root-links-<date>.json. All of it is one transaction.
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import String, Text, select  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402

from app.core.database import async_session, engine  # noqa: E402
from app.models.brand import Brand  # noqa: E402
from app.models.catalog import Category, Product  # noqa: E402
from app.models.content import HeroSlide, PromoBox, SiteContent  # noqa: E402
from app.models.legal import LegalDocument  # noqa: E402
from app.models.site import BannerPreset, PopupAnnouncement, SiteAnnouncement  # noqa: E402

CONTENT = (SiteContent, HeroSlide, PromoBox, SiteAnnouncement, PopupAnnouncement, BannerPreset, Category, Product, Brand, LegalDocument)

# The path at the start of a link: a bare path, or one on the shop's own domain
# (never /admin/catalog/… or another site's /catalog/…).
_LINK = re.compile(
    r"(?<![\w/.-])((?:https?://(?:www\.)?nargilemalzemesi\.com)?)/(?:catalog|product)/([a-z0-9]+(?:[.-][a-z0-9]+)*)(?=[/?#\"'\s)<\]]|$)"
)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def rewrite(text: str) -> str:
    """Every /catalog/<slug> and /product/<slug> link in `text` at its root address."""
    return _LINK.sub(lambda m: m.group(0) if _UUID.match(m.group(2)) else f"{m.group(1)}/{m.group(2)}", text)


def rewrite_value(value):
    """`rewrite` through a JSON document's strings, keys left as they are."""
    if isinstance(value, str):
        return rewrite(value)
    if isinstance(value, list):
        return [rewrite_value(v) for v in value]
    if isinstance(value, dict):
        return {k: rewrite_value(v) for k, v in value.items()}
    return value


def _columns(model):
    return [c for c in model.__table__.columns if isinstance(c.type, (String, Text, JSONB)) and not c.primary_key]


async def run(dry_run: bool) -> None:
    changes = []
    async with async_session() as db:
        for model in CONTENT:
            columns = _columns(model)
            rows = (await db.execute(select(model))).scalars().all()
            for row in rows:
                for column in columns:
                    before = getattr(row, column.key)
                    if before is None:
                        continue
                    after = rewrite_value(before)
                    if after != before:
                        setattr(row, column.key, after)
                        key = getattr(row, "id", None) or getattr(row, "key", None) or getattr(row, "slug", None)
                        changes.append({"table": model.__tablename__, "row": str(key), "column": column.key, "before": before, "after": after})
        by_table: dict[str, int] = {}
        for change in changes:
            by_table[change["table"]] = by_table.get(change["table"], 0) + 1
        print(f"values changed: {len(changes)}", by_table)
        for change in changes[:12]:
            links = sorted(set(_LINK.findall(json.dumps(change["before"], ensure_ascii=False))))
            print(f"  {change['table']}.{change['column']} [{change['row']}]: {['/'.join(l).strip('/') for l in links][:4]}")
        if dry_run:
            await db.rollback()
            print("dry run: nothing written")
            return
        await db.commit()
    out = Path(__file__).resolve().parent / "migration_reports" / f"root-links-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(changes, ensure_ascii=False, indent=1, default=str))
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
