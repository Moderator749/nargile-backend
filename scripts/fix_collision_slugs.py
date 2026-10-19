"""Give products whose slug carries a collision suffix ("-2", "-3"…) their
code-and-name address instead, keeping the old one as a redirect.

Before product slugs led with the product code, two products of one name met
at "<name>-2", "<name>-3"…; the old store did the same. This finds those, and
only those. A slug is flagged when all three hold:

1. it ends in "-<number>";
2. without that suffix it is exactly the slug of the product's own name — the
   number was put on top of the name, not part of it (so "wd-hookah-g48-12",
   where the name itself ends in "G48-12", is never flagged);
3. the number is nowhere in the product's code or name as a number of its own.

The new address is the one a new product gets (product_slug_base: code, then
name), or one picked by hand (OVERRIDES) where that would repeat a word. One whose new address would clash with another product's slug, another
proposal, or a redirect of another product is skipped and listed.

    cd saas_backend
    .venv/bin/python scripts/fix_collision_slugs.py            # dry run: prints, writes nothing
    .venv/bin/python scripts/fix_collision_slugs.py --commit   # applies, one transaction

Before anything else, product codes listed in CODE_FIXES are corrected (the
product's code and its colours' SKUs), so the new addresses carry the right code.

Commit mode records each old slug in the redirect table (slug_redirects.moved),
so every indexed or shared link keeps leading to its product, and writes a
JSON report to scripts/migration_reports/. Idempotent: a second run finds
nothing to change.
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.core.database import async_session, engine
from app.core.slugs import PRODUCT_SLUG, product_slug_base, slugify
from app.models.catalog import Product, ProductVariant, SlugRedirect
from app.services import slug_redirects

REPORTS = Path(__file__).resolve().parent / "migration_reports"
SUFFIX = re.compile(r"^(?P<base>.+)-(?P<n>\d+)$")

#: Product codes mistyped at import: wrong → right (product code and colour SKUs).
CODE_FIXES = {"intial-40": "initial-40"}

#: Addresses picked by hand, by product code, where code-then-name repeats a word.
OVERRIDES = {
    "ART BOWL 02": "art-bowl-02-killer-lule",
    "NA04 SARI": "na04-sari-liger-askili-sipsi",
}


def collision_suffix(slug: str, name: str, code: str) -> str | None:
    """The slug's collision number, or None when it is not clearly one."""
    m = SUFFIX.match(slug)
    if not m:
        return None
    if m["base"] != slugify(name):
        return None  # not the name plus a number: an old-store spelling, a rename, a model number
    numbers = set(re.findall(r"\d+", f"{name} {code}"))
    if m["n"] in numbers or m["n"].lstrip("0") in {x.lstrip("0") for x in numbers}:
        return None  # the number belongs to the product
    return m["n"]


async def run(commit: bool) -> dict:
    async with async_session() as db, db.begin():
        code_fixes = []
        for wrong, right in CODE_FIXES.items():
            product = await db.scalar(select(Product).where(Product.base_code == wrong))
            if product is None:
                continue
            if await db.scalar(select(Product.id).where(Product.base_code == right)) is not None:
                code_fixes.append({"old": wrong, "new": right, "skus": [], "skipped": f"“{right}” is another product's code"})
                continue
            variants = (await db.scalars(select(ProductVariant).where(ProductVariant.product_id == product.id))).all()
            skus = [(v, right + v.sku[len(wrong):]) for v in variants if v.sku == wrong or v.sku.startswith(f"{wrong}-")]
            code_fixes.append({"old": wrong, "new": right, "skus": [[v.sku, new] for v, new in skus]})
            product.base_code = right
            for v, new in skus:
                v.sku = new
        await db.flush()

        products = (await db.execute(select(Product.id, Product.base_code, Product.name, Product.slug))).all()
        redirects = dict((await db.execute(select(SlugRedirect.old_slug, SlugRedirect.product_id).where(SlugRedirect.kind == "product"))).all())
        live = {p.slug for p in products}

        changes, skipped = [], []
        proposed: set[str] = set()
        for p in sorted(products, key=lambda r: r.slug):
            n = collision_suffix(p.slug, p.name or "", p.base_code or "")
            if n is None:
                continue
            new = OVERRIDES.get(p.base_code or "") or product_slug_base(p.name or "", p.base_code or "")
            reason = None
            if not PRODUCT_SLUG.match(new):
                reason = f"“{new}” is not a valid address"
            elif new == p.slug:
                reason = "already in code-and-name form"
            elif new in live or new in proposed:
                reason = f"“{new}” is taken"
            elif new in redirects and redirects[new] != p.id:
                reason = f"“{new}” redirects to another product"
            if reason:
                skipped.append({"code": p.base_code, "slug": p.slug, "proposed": new, "reason": reason})
                continue
            proposed.add(new)
            changes.append({
                "id": str(p.id), "code": p.base_code, "name": p.name, "old": p.slug, "new": new, "suffix": n,
                "by_hand": (p.base_code or "") in OVERRIDES,
            })

        if commit and changes:
            for c in changes:
                product = await db.get(Product, c["id"])
                await slug_redirects.moved(db, "product", c["old"], c["new"], product_id=product.id)
                product.slug = c["new"]
        elif not commit:
            await db.rollback()

    await engine.dispose()
    return {"commit": commit, "at": datetime.now().isoformat(timespec="seconds"), "products": len(products),
            "code_fixes": code_fixes, "changes": changes, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--commit", action="store_true", help="apply the changes (default: dry run, nothing written)")
    args = parser.parse_args()
    result = asyncio.run(run(args.commit))

    mode = "COMMIT" if args.commit else "DRY RUN — nothing written"
    print(f"{mode}: {result['products']} products, {len(result['changes'])} to change, {len(result['skipped'])} skipped\n")
    for f in result["code_fixes"]:
        note = f"  — SKIPPED: {f['skipped']}" if f.get("skipped") else ""
        skus = ", ".join(f"{a} -> {b}" for a, b in f["skus"]) or "none"
        print(f"Code fix: {f['old']} -> {f['new']} (SKUs: {skus}){note}")
    if result["code_fixes"]:
        print()
    if result["changes"]:
        width = max(len(c["code"] or "") for c in result["changes"])
        print(f"{'Ürün Kodu'.ljust(width)} | Mevcut Slug -> Önerilen Slug")
        for c in result["changes"]:
            print(f"{(c['code'] or '').ljust(width)} | {c['old']} -> {c['new']}{'   (seçilmiş)' if c['by_hand'] else ''}")
    if result["skipped"]:
        print("\nSkipped (flagged, but not changed):")
        for s in result["skipped"]:
            print(f"  {s['code']} | {s['slug']} -> {s['proposed']}  ({s['reason']})")

    if args.commit:
        REPORTS.mkdir(exist_ok=True)
        path = REPORTS / f"fix_collision_slugs_{result['at'].replace(':', '')}.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"\nReport: {path}")


if __name__ == "__main__":
    main()
