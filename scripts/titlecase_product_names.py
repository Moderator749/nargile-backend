"""Product names in title case, Turkish-aware: "MR.WOOD NARGİLE TAKIMI -BLACK
CRAFT" → "Mr.Wood Nargile Takımı - Black Craft".

The old store typed most names in capitals, and capitals lose what lower case
needs: a plain "I" is "ı" in Turkish ("TAKIMI" → "Takımı") but "i" in English
("WHITE" → "White"). A word with a Turkish letter (Ç Ş Ğ Ü Ö İ) follows the
Turkish rules; any other word with a plain "I" is English unless it is in
TURKISH_DOTLESS. Model codes (anything with a digit: LH014, N5-Z, X31-2) and the
short abbreviations in KEEP_UPPER stay as they are; units go lower case
("500 gr"); the brands of the brand list are spelled as the list spells them
("Mr.Wood"). A dash between words becomes " - ".

    .venv/bin/python scripts/titlecase_product_names.py --dry-run   # show, write nothing
    .venv/bin/python scripts/titlecase_product_names.py             # write (backup first)
    .venv/bin/python scripts/titlecase_product_names.py --restore scripts/migration_reports/names-<date>.json

Every run writes the names it changes, before and after, to
scripts/migration_reports/names-<date>.json, and --restore puts them back.
All changes are one transaction. Note that the OpenCart migration tool run with
--overwrite writes the old store's spelling back.
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.database import async_session, engine  # noqa: E402
from app.models.brand import Brand  # noqa: E402
from app.models.catalog import Product  # noqa: E402

# Turkish words of the catalogue whose plain "I" is a dotless ı.
TURKISH_DOTLESS = {
    "ALTI", "ASKILI", "BANTI", "CAMI", "CONTASI", "GAZI", "GAZLI", "KAPAKLI", "KIRMIZI",
    "KIZILAY", "MANZARALI", "MATI", "NTAKIMI", "OYMALI", "POMPASI", "SAPLI", "SIRLI",
    "TAKIM", "TAKIMI", "TAVASI", "TIPASI", "TIRTIKLI", "YAYI",
}
# Abbreviations and model letters that stay in capitals.
KEEP_UPPER = {
    "LED", "WD", "BY", "SY", "GBY", "GSY", "BK", "RS", "PVD", "KRD", "ATH", "WOH", "DMK",
    "MRA", "KZT", "KTS", "HMS", "NFX", "FNX", "YP", "OD", "SS", "CX", "PK", "WG", "WW",
    "WZ", "XL", "XS", "LH", "BS", "SF", "GT", "DSY", "MYT", "SYL", "LOF", "ENO", "OR",
    "REF", "INF", "NO", "CLP", "QM", "MTS", "RTK", "NT", "DNT", "UV", "USB", "BBQ",
}
# Misspellings of the old store, fixed on the way, and one spelling for a name
# typed two ways (the brand's own: MattPear, Darkside).
CORRECTIONS = {
    "orjınal": "Orjinal",
    "sipisi": "Sipsi",
    "temzileme": "Temizleme",
    "elektirikli": "Elektrikli",
    "difuzor": "Difüzör",
    "ntakımı": "Takımı",
    "desing": "Design",
    "adition": "Edition",
    "metalic": "Metallic",
    "rovalver": "Revolver",
    "celik": "Çelik",
    "lule": "Lüle",
    "marpuc": "Marpuç",
    "ortaboy": "Orta Boy",
    "pk": "PK",
    "dmk": "DMK",
    "nfx": "NFX",
    "mattpear": "MattPear",
    "darkside": "Darkside",
    "mocamp": "MoCamp",
}
LOWER = {"GR": "gr", "KG": "kg", "MM": "mm", "CM": "cm", "ML": "ml", "VE": "ve", "ER": "er", "LI": "li"}

TR_UPPER = str.maketrans({"i": "İ", "ı": "I"})
TR_LOWER = str.maketrans({"İ": "i", "I": "ı"})
TURKISH_LETTERS = re.compile(r"[ÇŞĞÜÖİçşğüöı]")


def _lower(word: str, turkish: bool) -> str:
    return word.translate(TR_LOWER).lower() if turkish else word.replace("İ", "i").lower()


def _upper_first(word: str, turkish: bool) -> str:
    first = word[0].translate(TR_UPPER).upper() if turkish else word[0].upper()
    return first + word[1:]


def title_word(word: str, brands: dict[str, str]) -> str:
    """One word (no spaces, no dashes) in title case."""
    core = word.strip("()'\".,:;!?")
    if not core:
        return word
    lead, trail = word[: word.index(core)], word[word.index(core) + len(core):]
    key = core.upper().translate(TR_UPPER) if not TURKISH_LETTERS.search(core) else core.translate(TR_UPPER).upper()
    folded = re.sub(r"[^0-9a-z]", "", core.lower().replace("ı", "i").replace("i̇", "i"))
    if core.lower() in CORRECTIONS:
        out = CORRECTIONS[core.lower()]
    elif folded in brands:
        out = brands[folded]
    elif re.search(r"\d", core) or key in KEEP_UPPER or (len(core) == 1 and core.isalpha()):
        out = core.upper() if core.isupper() or re.search(r"\d", core) else core
    elif key in LOWER:
        out = LOWER[key]
    elif "'" in core:  # "COCO's": the part before the apostrophe only
        head, _, tail = core.partition("'")
        out = title_word(head, brands) + "'" + tail.lower()
    elif not core.isupper():
        # Typed in mixed case already ("Steamulation", "marpuç"): only the first letter.
        out = _upper_first(core, bool(TURKISH_LETTERS.search(core)))
    else:
        turkish = bool(TURKISH_LETTERS.search(core)) or key in TURKISH_DOTLESS
        lowered = _lower(core, turkish)
        out = _upper_first(lowered, turkish)
    return lead + out + trail


def title_case(name: str, brands: dict[str, str]) -> str:
    text = re.sub(r"\s+", " ", name).strip()
    # A dash that separates words ("TAKIMI -BLACK", "Pure- Nargile",
    # "NARGİLE-ÇANTALI") becomes " - "; a code's dash ("N5-Z", "66-5") stays.
    text = re.sub(r"\s*[-–]\s+|\s+[-–]\s*", " - ", text)
    text = re.sub(r"(?<=[^\W\d_]{3})-(?=[^\W\d_]{3})", " - ", text)
    parts = re.split(r"(\s+|-|/)", text)
    out = "".join(p if re.fullmatch(r"\s+|-|/", p) else title_word(p, brands) for p in parts)
    return re.sub(r"\s+", " ", out).strip()


async def run(opts) -> None:
    async with async_session() as db:
        brands = {
            re.sub(r"[^0-9a-z]", "", n.lower()): n for (n,) in (await db.execute(select(Brand.name))).all()
        }
        if opts.restore:
            saved = json.loads(Path(opts.restore).read_text())
            for product in (await db.execute(select(Product).where(Product.id.in_(list(saved))))).scalars():
                product.name = saved[str(product.id)]["before"]
            await db.commit()
            print(f"restored {len(saved)} names from {opts.restore}")
            return

        changes = {}
        for product in (await db.execute(select(Product).order_by(Product.name))).scalars():
            new = title_case(product.name, brands)[:255]
            if new != product.name:
                changes[str(product.id)] = {"before": product.name, "after": new}
                product.name = new
        for c in list(changes.values())[: opts.show]:
            print(f"  {c['before'][:70]:<70} → {c['after']}")
        print(f"{len(changes)} names to change")
        if opts.dry_run:
            await db.rollback()
            return
        path = Path(__file__).parent / "migration_reports" / f"names-{datetime.now():%Y%m%d-%H%M%S}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(changes, ensure_ascii=False, indent=2))
        await db.commit()
        print(f"written; before/after saved to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Product names in Turkish-aware title case.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--show", type=int, default=40, help="how many changes to print")
    parser.add_argument("--restore", metavar="JSON", help="put back the names a previous run saved")
    opts = parser.parse_args()
    engine.echo = False
    asyncio.run(run(opts))


if __name__ == "__main__":
    main()
