"""Give imported products the old store's own address as their slug.

The old store (OpenCart, https://www.nargilemalzemesi.com) served each product
at /<seo-keyword>; the migration reports written by opencart_migration_tool.py
record that address next to each product code. A product whose code is in a
report takes the address's last segment as its slug, exactly as the old store
spelled it ("mr.wood-nargile-takimi-…" keeps its dot), so the product keeps the
name search engines already know it by. Products with no report entry keep the
slug the migration derived from their name.

    cd saas_backend
    .venv/bin/python scripts/apply_legacy_product_slugs.py --dry-run
    .venv/bin/python scripts/apply_legacy_product_slugs.py

Idempotent: a second run finds nothing to change. One transaction: all or
nothing. Writes a JSON report to scripts/migration_reports/.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, update

from app.core.database import async_session, engine
from app.core.slugs import PRODUCT_SLUG, PRODUCT_SLUG_MAX
from app.models.catalog import Product

REPORTS = Path(__file__).resolve().parent / "migration_reports"


def legacy_slugs() -> tuple[dict[str, str], list[str]]:
    """Product code (lowercase) → old-store slug, from every real (non-dry) run."""
    by_code: dict[str, str] = {}
    rejected: list[str] = []
    for path in sorted(REPORTS.glob("opencart-*.json")):
        report = json.loads(path.read_text())
        if report.get("dry_run"):
            continue
        for outcome in report.get("outcomes", []):
            if outcome.get("action") not in ("created", "updated", "unchanged") or not outcome.get("code"):
                continue
            slug = urlparse(outcome["url"]).path.strip("/").rsplit("/", 1)[-1].lower()
            if not PRODUCT_SLUG.match(slug) or len(slug) > PRODUCT_SLUG_MAX:
                rejected.append(outcome["url"])
                continue
            by_code.setdefault(outcome["code"].lower(), slug)
    return by_code, rejected


async def run(dry_run: bool) -> dict:
    by_code, rejected = legacy_slugs()
    try:
        async with async_session() as db, db.begin():
            products = (await db.execute(select(Product.id, Product.base_code, Product.slug))).all()
            wanted = {p.id: by_code[p.base_code.lower()] for p in products if p.base_code.lower() in by_code}
            # Two codes of the old store pointing at one address: neither gets it.
            counts: dict[str, int] = {}
            for slug in wanted.values():
                counts[slug] = counts.get(slug, 0) + 1
            wanted = {pid: slug for pid, slug in wanted.items() if counts[slug] == 1}
            changes = {pid: slug for pid, slug in wanted.items() if next(p.slug for p in products if p.id == pid) != slug}
            # A product outside the import holding an address an imported one
            # needs: reported, and that imported product is left alone.
            outside = {p.slug: p for p in products if p.id not in wanted}
            blocked = {pid: slug for pid, slug in changes.items() if slug in outside}
            changes = {pid: slug for pid, slug in changes.items() if pid not in blocked}
            if changes and not dry_run:
                # Through a placeholder first: one product's new slug may be
                # another's current one, and the index is unique at every step.
                for pid in changes:
                    await db.execute(update(Product).where(Product.id == pid).values(slug=f"tmp-{pid}"))
                for pid, slug in changes.items():
                    await db.execute(update(Product).where(Product.id == pid).values(slug=slug))
            if dry_run:
                await db.rollback()
        summary = {
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "dry_run": dry_run,
            "products": len(products),
            "with_legacy_slug": len(wanted),
            "changed": len(changes),
            "already_legacy": len(wanted) - len(changes) - len(blocked),
            "without_legacy_slug": len(products) - len(wanted),
            "blocked_by_other_product": [{"product_id": str(pid), "slug": s} for pid, s in blocked.items()],
            "rejected_addresses": rejected,
            "duplicated_addresses": sorted(s for s, n in counts.items() if n > 1),
            "samples": [
                {"product_id": str(pid), "from": next(p.slug for p in products if p.id == pid), "to": slug}
                for pid, slug in list(changes.items())[:10]
            ],
        }
        return summary
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = parser.parse_args()
    summary = asyncio.run(run(args.dry_run))
    out = REPORTS / f"legacy-slugs-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "samples"}, ensure_ascii=False, indent=2))
    print(f"report: {out}")


if __name__ == "__main__":
    main()
