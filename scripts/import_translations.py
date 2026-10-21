"""Load English and Arabic translations of products and categories from JSON.

Products: one or more files, each a JSON array of
    {"id": "<product uuid>", "en": {"name", "description", "meta_title", "meta_description"}, "ar": {...}}
Categories (--categories): one file, a JSON array of
    {"slug": "<category slug>", "en": {"name", "description"}, "ar": {...}}

Every entry is validated with the same schemas the admin panel uses
(app.schemas.i18n: lengths, and descriptions through the rich-text sanitizer)
before anything is written. A language in the file replaces that language on
the row; a language the file leaves out is kept. An id or slug the database
does not know is listed and skipped.

    cd saas_backend
    .venv/bin/python scripts/import_translations.py translations_*.json --categories categories.json            # dry run
    .venv/bin/python scripts/import_translations.py translations_*.json --categories categories.json --commit   # one transaction

Idempotent: running it again with the same files changes nothing.
"""

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import TypeAdapter, ValidationError  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.models.catalog import Category, Product  # noqa: E402
from app.schemas.i18n import CategoryTranslations, ProductTranslations, stored  # noqa: E402

PRODUCT_ROW = TypeAdapter(ProductTranslations)
CATEGORY_ROW = TypeAdapter(CategoryTranslations)


def _languages(entry: dict) -> dict:
    return {lang: entry[lang] for lang in ("en", "ar") if lang in entry}


def _merge(current: dict | None, incoming: dict | None) -> dict | None:
    merged = {**(current or {}), **(incoming or {})}
    return merged or None


async def run(product_files: list[Path], category_file: Path | None, commit: bool) -> dict:
    report = {"products": {"updated": 0, "unchanged": 0, "unknown": [], "invalid": []},
              "categories": {"updated": 0, "unchanged": 0, "unknown": [], "invalid": []}}

    product_entries: dict[uuid.UUID, dict] = {}
    for path in product_files:
        for entry in json.loads(path.read_text()):
            try:
                product_entries[uuid.UUID(entry["id"])] = stored(PRODUCT_ROW.validate_python(_languages(entry)))
            except (ValidationError, KeyError, ValueError) as exc:
                report["products"]["invalid"].append({"id": entry.get("id"), "error": str(exc)[:300]})

    category_entries: dict[str, dict] = {}
    if category_file:
        for entry in json.loads(category_file.read_text()):
            try:
                category_entries[entry["slug"]] = stored(CATEGORY_ROW.validate_python(_languages(entry)))
            except (ValidationError, KeyError) as exc:
                report["categories"]["invalid"].append({"slug": entry.get("slug"), "error": str(exc)[:300]})

    # An engine of its own, closed at the end: nothing shared with whatever
    # else runs in this process (the app's pool, a test's).
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await _apply(db, product_entries, category_entries, report, commit)
    finally:
        await engine.dispose()
    return report


async def _apply(db, product_entries: dict, category_entries: dict, report: dict, commit: bool) -> None:
    if product_entries:
        rows = (await db.scalars(select(Product).where(Product.id.in_(product_entries)))).all()
        found = {p.id: p for p in rows}
        report["products"]["unknown"] = [str(i) for i in product_entries if i not in found]
        for pid, incoming in product_entries.items():
            product = found.get(pid)
            if product is None:
                continue
            merged = _merge(product.translations, incoming)
            if merged == product.translations:
                report["products"]["unchanged"] += 1
            else:
                product.translations = merged
                report["products"]["updated"] += 1
    if category_entries:
        rows = (await db.scalars(select(Category).where(Category.slug.in_(category_entries)))).all()
        found_c = {c.slug: c for c in rows}
        report["categories"]["unknown"] = [s for s in category_entries if s not in found_c]
        for slug, incoming in category_entries.items():
            category = found_c.get(slug)
            if category is None:
                continue
            merged = _merge(category.translations, incoming)
            if merged == category.translations:
                report["categories"]["unchanged"] += 1
            else:
                category.translations = merged
                report["categories"]["updated"] += 1
    if commit:
        await db.commit()
    else:
        await db.rollback()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("products", nargs="*", type=Path, help="product translation files (JSON arrays)")
    parser.add_argument("--categories", type=Path, help="category translation file (JSON array)")
    parser.add_argument("--commit", action="store_true", help="write the changes (default: dry run, nothing written)")
    args = parser.parse_args()
    report = asyncio.run(run(args.products, args.categories, args.commit))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("COMMITTED" if args.commit else "DRY RUN — nothing written (add --commit)")
    if report["products"]["invalid"] or report["categories"]["invalid"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
