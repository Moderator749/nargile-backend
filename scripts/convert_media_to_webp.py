"""Every JPEG and PNG already under MEDIA_ROOT as a WebP, and every address to
it in the database pointing at the WebP.

New uploads are stored as WebP (app.services.media.optimise_photo); this
brings the files from before up to them, the same way: product photos up to
1800px on their longest side, slides, promo boxes and logos up to 2560px,
quality 82 (lossless where there is transparency), upright and without EXIF.

Three steps, each run on its own:

    # 1. What would change — nothing is written
    .venv/bin/python scripts/convert_media_to_webp.py

    # 2. Write each "x-1a2b3c4d.webp" beside its "x-1a2b3c4d.jpg" and point the
    #    database at it (one transaction). The originals stay. Safe to run again:
    #    a WebP already there is used, not made twice.
    .venv/bin/python scripts/convert_media_to_webp.py --apply

    # 3. Once the site has been checked: remove each original that has its WebP
    #    and that nothing in the database names any more.
    .venv/bin/python scripts/convert_media_to_webp.py --delete-originals

An address to a removed original keeps working: /media answers it with a 301
to its WebP (app.main), for search engines and old e-mails.

The database is rewritten in the shop's content only — the catalog, the
homepage and its boxes, slides, announcements, legal texts — where every text
and JSON column is searched for "<folder>/<file>.jpg|jpeg|png" (inside a full
URL or alone, as brands.logo_path is). Orders keep what they recorded; --delete-
originals looks through every table before it removes a file anything names.
"""

import argparse
import asyncio
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import async_session  # noqa: E402
from app.services.media import MAX_SIDE, WIDE_SIDE, InvalidImage, optimise_photo  # noqa: E402

CONTENT_TABLES = (
    "products",
    "product_variants",
    "categories",
    "brands",
    "hero_slides",
    "promo_boxes",
    "site_content",
    "site_announcements",
    "popup_announcements",
    "banner_presets",
    "legal_documents",
)
OLD_SUFFIXES = (".jpg", ".jpeg", ".png")


def _side(rel: str) -> int:
    """Product photos as uploads are kept; everything drawn wide, wider."""
    return MAX_SIDE if rel.startswith("products/") else WIDE_SIDE


def _originals(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in OLD_SUFFIXES)


def _reference_pattern(root: Path) -> re.Pattern:
    """"<folder>/<name>.jpg|jpeg|png" for any folder under the media root, as it sits in a URL or alone."""
    folders = sorted(p.name for p in root.iterdir() if p.is_dir())
    names = "|".join(re.escape(f) for f in folders) or "products"
    return re.compile(rf"(?<![\w-])((?:{names})/[\w.\-]+\.(?:jpe?g|png))(?![\w])", re.IGNORECASE)


def convert(root: Path, originals: list[Path], apply: bool) -> tuple[dict[str, str], list[str], int, int]:
    """Each original's WebP (written with `apply`): {old rel: new rel}, failures, bytes before, bytes after."""
    mapping: dict[str, str] = {}
    failed: list[str] = []
    before = after = 0
    started = time.monotonic()
    for i, path in enumerate(originals, start=1):
        rel = path.relative_to(root).as_posix()
        target = path.with_suffix(".webp")
        new_rel = target.relative_to(root).as_posix()
        if not apply:
            mapping[rel] = new_rel
            continue
        try:
            if not target.exists():
                data = optimise_photo(path.read_bytes(), _side(rel))
                partial = target.with_suffix(".webp.part")
                partial.write_bytes(data)
                partial.replace(target)
            before += path.stat().st_size
            after += target.stat().st_size
            mapping[rel] = new_rel
        except (InvalidImage, OSError) as exc:
            failed.append(f"{rel}: {exc}")
        if i % 50 == 0 or i == len(originals):
            print(f"  {i}/{len(originals)} files ({time.monotonic() - started:.0f}s)", flush=True)
    return mapping, failed, before, after


async def _columns(session, tables) -> list[tuple[str, str, str]]:
    query = (
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND data_type IN ('text', 'character varying', 'jsonb')"
    )
    rows = (await session.execute(text(query))).all()
    return [(t, c, d) for t, c, d in rows if tables is None or t in tables]


async def _rows_naming_images(session, table: str, column: str, data_type: str):
    value = f'"{column}"::text' if data_type == "jsonb" else f'"{column}"'
    query = f"SELECT ctid::text, {value} FROM \"{table}\" WHERE {value} ~* '\\.(jpe?g|png)'"
    return (await session.execute(text(query))).all()


async def rewrite(mapping: dict[str, str], pattern: re.Pattern, apply: bool) -> dict[str, int]:
    """Point every address the database has to an original at its WebP; {table.column: rows changed}."""
    lowered = {k.lower(): v for k, v in mapping.items()}
    changed: dict[str, int] = {}
    async with async_session() as session:
        for table, column, data_type in await _columns(session, CONTENT_TABLES):
            for ctid, value in await _rows_naming_images(session, table, column, data_type):
                new = pattern.sub(lambda m: lowered.get(m.group(1).lower(), m.group(1)), value)
                if new == value:
                    continue
                changed[f"{table}.{column}"] = changed.get(f"{table}.{column}", 0) + 1
                if apply:
                    cast = "CAST(:v AS jsonb)" if data_type == "jsonb" else ":v"
                    await session.execute(
                        text(f'UPDATE "{table}" SET "{column}" = {cast} WHERE ctid::text = :ctid'),
                        {"v": new, "ctid": ctid},
                    )
        if apply:
            await session.commit()
    return changed


async def still_named(pattern: re.Pattern) -> set[str]:
    """Every original something in the database still names — any table, orders included."""
    named: set[str] = set()
    async with async_session() as session:
        for table, column, data_type in await _columns(session, None):
            for _, value in await _rows_naming_images(session, table, column, data_type):
                named.update(m.group(1).lower() for m in pattern.finditer(value))
    return named


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    step = parser.add_mutually_exclusive_group()
    step.add_argument("--apply", action="store_true", help="write the WebPs and point the database at them")
    step.add_argument("--delete-originals", action="store_true", help="remove originals that have their WebP and are named nowhere")
    parser.add_argument("--limit", type=int, default=None, help="only the first N files (a trial run)")
    args = parser.parse_args()

    root = settings.MEDIA_ROOT.resolve()
    if not root.is_dir():
        print(f"MEDIA_ROOT not found: {root}")
        return 1
    pattern = _reference_pattern(root)
    originals = _originals(root)[: args.limit]
    print(f"Media root: {root} — {len(originals)} JPEG/PNG file(s)")

    if args.delete_originals:
        named = await still_named(pattern)
        removed = kept_named = kept_missing = freed = 0
        for path in originals:
            rel = path.relative_to(root).as_posix()
            if not path.with_suffix(".webp").exists():
                kept_missing += 1
            elif rel.lower() in named:
                kept_named += 1
            else:
                freed += path.stat().st_size
                path.unlink()
                removed += 1
        print(f"Removed {removed} original(s), {freed / 1_048_576:.1f} MB freed.")
        print(f"Kept {kept_named} still named in the database, {kept_missing} without a WebP (run --apply first).")
        return 0

    mapping, failed, before, after = convert(root, originals, args.apply)
    changed = await rewrite(mapping, pattern, args.apply)
    if args.apply:
        print(f"Converted {len(mapping)} file(s): {before / 1_048_576:.1f} MB → {after / 1_048_576:.1f} MB.")
    else:
        print(f"Would convert {len(mapping)} file(s). (Dry run: nothing written.)")
    for where, n in sorted(changed.items()):
        print(f"  {'updated' if args.apply else 'would update'} {n:>5} row(s) in {where}")
    if failed:
        print(f"{len(failed)} file(s) could not be read and were left as they are:")
        for line in failed:
            print(f"  {line}")
    if args.apply:
        print("Next, once the site has been checked: --delete-originals")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
