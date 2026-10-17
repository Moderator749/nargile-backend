"""Bulk prices from the supplier's pricing workbook (the "01a-SULTAN" sheet).

The sheet is a stack of four-row blocks, two side by side at the top:

    header  <name>  HAM MALİYET | KDV DHL | ÖZEL FİYAT | … | BAYİ | TOPTAN | KAFE | MAĞAZA | PERAKENDE
    +1      the calculated chain, unrounded
    +2      the rounded prices — the ones charged
    +3      (blank)

The left block has its name in column 0 and BAYİ, TOPTAN, KAFE, PERAKENDE in
columns 6, 9, 12 and 17; the right block the same from column 19 (25, 28, 31,
36). MAĞAZA is no customer group and is left out.

A block is found by its header labels, never by text in the name column alone:
the row above a section's first block carries the section's title ("SULTAN
ÇANTALI"), which is no product, and two rows below *that* is the unrounded
chain, not a price.

Only blocks in TL with VAT included are read — the top of the workbook. Further
down the same sheet prices are in dollars or euros before VAT, with a "TL"
column beside them and often no BAYİ column. Read as TL they would be charged at
a fiftieth of their price, so they are reported as skipped and left alone.

Names are product codes ("LH013 GOLD", "NT745 SY"). A row is priced without
asking only when its name is certain:

1. a name the admin has tied to a product before (`PriceImportAlias`);
2. exactly — case, spaces and punctuation aside — a product's code, one of its
   colours' SKUs, or its whole name.

Anything looser is a *suggestion*, priced only once the admin confirms it (which
stores the alias, so it is certain next time): the name with its words spelled
the other way ("NT10 GOLD" for "NT10 G"), or a code like it. Suggestions keep
to the same model number — the codes differ by a digit (LH013/LH014,
NT750/NT770), and text similarity alone would price "NT10 GOLD" as the NT120
GOLD or "LH014" as the LH014 *bottle*. Never by a name merely containing the
code either: "NT130" would find the NT130 bottle.
"""

import difflib
import io
import itertools
import re
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.catalog import CurrencyType, Product, VariantPrice
from app.models.pricing import PriceImportAlias
from app.models.user import SegmentType
from app.services.pricing_sheet import CENT, segment_ids, write_prices

# Offsets from a block's name column.
PRICE_COLUMNS = {
    SegmentType.DEALER: 6,
    SegmentType.WHOLESALE: 9,
    SegmentType.CAFE: 12,
    SegmentType.RETAIL: 17,
}
HEADER_LABELS = {
    SegmentType.DEALER: "BAY",
    SegmentType.WHOLESALE: "TOPTAN",
    SegmentType.CAFE: "KAFE",
    SegmentType.RETAIL: "PERAKENDE",
}
SIDES = (("sol", 0), ("sağ", 19))
PRICE_ROW_OFFSET = 2
# Beside a foreign-currency block, the column after PERAKENDE is its TL value.
TL_COLUMN_OFFSET = 19
FOREIGN_WORDS = ("EURO", "EUR", "DOLAR", "USD", "$", "€")
DEFAULT_VAT = Decimal("20")

# Words the workbook and the catalog spell differently; a name is also tried
# with each word swapped for its other spellings (suggestions only).
ABBREVIATIONS = [
    {"GOLD", "G", "ALTIN"},
    {"SIYAH", "SY"},
    {"KIRMIZI", "KR"},
    {"BEYAZ", "BY"},
    {"GUNMETAL", "GM"},
    {"MINI", "M"},
    {"CELIK", "ÇELIK"},
]
SUGGESTIONS = 3
# Below this, a name with no model number has no suggestion at all.
LOOSE_CUTOFF = 0.8


class ImportFileError(ValueError):
    pass


@dataclass
class Block:
    sheet: str
    row: int  # 1-based, the header (= name) row
    side: str
    section: str | None
    name: str
    prices: dict[SegmentType, Decimal | None]


@dataclass
class Skipped:
    sheet: str
    row: int
    side: str
    name: str
    reason: str  # "foreign_currency" | "vat_excluded" | "no_dealer_price"


@dataclass
class Parsed:
    blocks: list[Block] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"\s+", " ", str(value)).strip()


def _upper(value) -> str:
    return _text(value).replace("i", "İ").upper()


def normalize(value) -> str:
    """A code as compared: upper case, letters and digits only ("LH031-V1" = "lh031 v1")."""
    text = unicodedata.normalize("NFKC", _text(value)).replace("İ", "I").replace("ı", "I").upper()
    return re.sub(r"[^0-9A-ZÇĞÖŞÜ]", "", text)


def _price(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        price = Decimal(str(value)).quantize(CENT)
    except InvalidOperation:
        return None
    return price if price > 0 else None


def parse_workbook(data: bytes) -> Parsed:
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, KeyError, OSError) as exc:
        raise ImportFileError("Dosya okunamadı: .xlsx biçiminde bir Excel dosyası yükleyin.") from exc

    parsed = Parsed()
    for sheet in workbook.worksheets:
        grid = [list(row) for row in sheet.iter_rows(values_only=True)]

        def cell(r: int, c: int):
            return grid[r][c] if 0 <= r < len(grid) and 0 <= c < len(grid[r]) else None

        def has_label(r: int, base: int, segment: SegmentType) -> bool:
            return HEADER_LABELS[segment] in _upper(cell(r, base + PRICE_COLUMNS[segment]))

        def is_header(r: int, base: int) -> bool:
            """Every group's label over its column; without BAYİ it is still a block, but not one to read."""
            return all(has_label(r, base, seg) for seg in PRICE_COLUMNS if seg != SegmentType.DEALER)

        headers = {(r, base) for r in range(len(grid)) for _, base in SIDES if is_header(r, base)}
        section: dict[int, str | None] = {base: None for _, base in SIDES}
        for r in range(len(grid)):
            for side, base in SIDES:
                name = _text(cell(r, base))
                if (r, base) not in headers:
                    # A title on its own row, outside any block, names the section below it.
                    in_block = (r - 1, base) in headers or (r - 2, base) in headers
                    if name and not in_block and not isinstance(cell(r, base), (int, float)):
                        section[base] = name
                    continue
                data_row = [_upper(cell(r + 1, base + k)) for k in range(PRICE_COLUMNS[SegmentType.RETAIL] + 1)]
                if _upper(cell(r, base + TL_COLUMN_OFFSET)) == "TL" or any(
                    word in text for text in data_row for word in FOREIGN_WORDS
                ):
                    parsed.skipped.append(Skipped(sheet.title, r + 1, side, name, "foreign_currency"))
                    continue
                if "HARİÇ" in data_row[2] or "HARIC" in data_row[2]:
                    parsed.skipped.append(Skipped(sheet.title, r + 1, side, name, "vat_excluded"))
                    continue
                if not has_label(r, base, SegmentType.DEALER):
                    parsed.skipped.append(Skipped(sheet.title, r + 1, side, name, "no_dealer_price"))
                    continue
                prices = {seg: _price(cell(r + PRICE_ROW_OFFSET, base + col)) for seg, col in PRICE_COLUMNS.items()}
                parsed.blocks.append(Block(sheet.title, r + 1, side, section[base], name, prices))
    return parsed


@dataclass
class Match:
    block: Block
    product: Product
    matched_by: str  # "alias" | "code" | "sku" | "name"
    vat_rate: Decimal
    old_prices: dict[SegmentType, tuple[CurrencyType, Decimal]]
    alias_id: int | None = None


@dataclass
class Candidate:
    product: Product
    score: float  # 0–1
    basis: str  # "abbreviation" | "similar" | "same_code"
    retail: tuple[CurrencyType, Decimal] | None


@dataclass
class Suggested:
    block: Block
    candidates: list[Candidate]


@dataclass
class Miss:
    block: Block
    reason: str  # "unnamed" | "incomplete" | "not_found" | "conflict" | "no_variants"


@dataclass
class Preview:
    matched: list[Match]
    suggested: list[Suggested]
    unmatched: list[Miss]


WORD = re.compile(r"[A-ZÇĞÖŞÜ]+\d+|\d+|[A-ZÇĞÖŞÜ]+")


def _words(key: str) -> list[str]:
    """A normalised name in its parts: "NT10GOLD" -> NT10, GOLD; "LH031V1GM" -> LH031, V1, GM."""
    return WORD.findall(key)


def _model(key: str) -> str | None:
    """The model number a code starts with ("NT10" of "NT10GOLD"); suggestions keep to it."""
    words = _words(key)
    return words[0] if words and re.fullmatch(r"[A-ZÇĞÖŞÜ]+\d+", words[0]) else None


def _same_model(model: str, key: str) -> bool:
    """`key` is that model, not one whose number merely starts the same (NT10 is not NT1002)."""
    return key.startswith(model) and not key[len(model):len(model) + 1].isdigit()


def spellings(key: str) -> set[str]:
    """The name with each word in its other spellings: "NT10GOLD" -> also "NT10G", "NT10ALTIN"."""
    options = []
    for word in _words(key):
        alternatives = {word}
        for group in ABBREVIATIONS:
            if word in group:
                alternatives |= group
        options.append(sorted(alternatives))
    return {"".join(parts) for parts in itertools.islice(itertools.product(*options), 64)}


async def match_blocks(db: AsyncSession, parsed: Parsed) -> Preview:
    products = (await db.scalars(select(Product).options(selectinload(Product.variants)))).all()
    product_by_id = {p.id: p for p in products}
    by: dict[str, dict[str, set]] = {"code": defaultdict(set), "sku": defaultdict(set), "name": defaultdict(set)}
    keys_of: dict = defaultdict(set)  # product -> its code and SKUs, normalised
    for product in products:
        by["code"][normalize(product.base_code)].add(product.id)
        by["name"][normalize(product.name)].add(product.id)
        keys_of[product.id].add(normalize(product.base_code))
        for variant in product.variants:
            by["sku"][normalize(variant.sku)].add(product.id)
            keys_of[product.id].add(normalize(variant.sku))
    aliases = {
        a.key: a for a in (await db.scalars(select(PriceImportAlias))).all() if a.product_id in product_by_id
    }

    variant_ids = [v.id for p in products for v in p.variants if v.is_active]
    rows = (await db.scalars(select(VariantPrice).where(VariantPrice.variant_id.in_(variant_ids)))).all() if variant_ids else []
    segments = {seg_id: seg for seg, seg_id in (await segment_ids(db)).items()}
    prices_by_variant: dict = defaultdict(list)
    for row in rows:
        prices_by_variant[row.variant_id].append(row)

    def current(product: Product) -> tuple[dict[SegmentType, tuple[CurrencyType, Decimal]], Decimal | None]:
        """Its prices now (from its first active colour; a TRY row wins, as at checkout) and its VAT rate."""
        active = [v for v in product.variants if v.is_active]
        old: dict[SegmentType, tuple[CurrencyType, Decimal]] = {}
        vat_rate = None
        for row in prices_by_variant[active[0].id] if active else []:
            segment = segments.get(row.segment_id)
            if segment is None:
                continue
            if segment not in old or CurrencyType(row.currency) == CurrencyType.TRY:
                old[segment] = (CurrencyType(row.currency), row.price_incl_tax)
            if segment == SegmentType.RETAIL:
                vat_rate = row.vat_rate
        return old, vat_rate

    def candidate(product: Product, score: float, basis: str) -> Candidate:
        return Candidate(product, round(score, 2), basis, current(product)[0].get(SegmentType.RETAIL))

    def suggest(code: str) -> list[Candidate]:
        names = spellings(code)
        model = _model(code)
        if model:
            pool = [p for p in products if any(_same_model(model, key) for key in keys_of[p.id])]
            scored = []
            for product in pool:
                keys = keys_of[product.id]
                if names & keys:
                    scored.append(candidate(product, 1.0, "abbreviation"))
                else:
                    best = max(difflib.SequenceMatcher(None, n, k).ratio() for n in names for k in keys)
                    scored.append(candidate(product, best, "similar"))
            return sorted(scored, key=lambda c: (-c.score, c.product.base_code))[:SUGGESTIONS]
        # No model number to hold to ("AMY STAR"): only a close likeness, by code or name.
        found: dict = {}
        for product in products:
            keys = keys_of[product.id] | {normalize(product.name)}
            best = max(difflib.SequenceMatcher(None, n, k).ratio() for n in names for k in keys)
            if best >= LOOSE_CUTOFF:
                found[product.id] = candidate(product, best, "similar")
        return sorted(found.values(), key=lambda c: (-c.score, c.product.base_code))[:SUGGESTIONS]

    suggested: list[Suggested] = []
    unmatched: list[Miss] = []
    # The same name twice with different prices cannot be applied either way.
    by_name: dict[str, list[Block]] = defaultdict(list)
    for block in parsed.blocks:
        if not normalize(block.name):
            unmatched.append(Miss(block, "unnamed"))
        elif any(price is None for price in block.prices.values()):
            unmatched.append(Miss(block, "incomplete"))
        else:
            by_name[normalize(block.name)].append(block)

    found: list[tuple[Block, Product, str, int | None]] = []
    for code, blocks in by_name.items():
        if len({tuple(b.prices.items()) for b in blocks}) > 1:
            unmatched.extend(Miss(b, "conflict") for b in blocks)
            continue
        block = blocks[0]  # repeats of the same prices are one row
        if code in aliases:
            alias = aliases[code]
            found.append((block, product_by_id[alias.product_id], "alias", alias.id))
            continue
        ids = by["code"][code] | by["sku"][code]
        matched_by = "code" if by["code"][code] else "sku"
        if not ids:
            ids, matched_by = by["name"][code], "name"
        if len(ids) == 1:
            found.append((block, product_by_id[next(iter(ids))], matched_by, None))
        elif ids:
            same = sorted((product_by_id[i] for i in ids), key=lambda p: p.base_code)
            suggested.append(Suggested(block, [candidate(p, 1.0, "same_code") for p in same]))
        elif candidates := suggest(code):
            suggested.append(Suggested(block, candidates))
        else:
            unmatched.append(Miss(block, "not_found"))

    per_product: dict = defaultdict(list)
    for entry in found:
        per_product[entry[1].id].append(entry)

    matched: list[Match] = []
    for entries in per_product.values():
        # Two rows landing on one product: fine when they agree (one model in
        # two colour rows), neither wins when they do not.
        if len({tuple(block.prices.items()) for block, _, _, _ in entries}) > 1:
            unmatched.extend(Miss(block, "conflict") for block, _, _, _ in entries)
            continue
        block, product, matched_by, alias_id = entries[0]
        if not any(v.is_active for v in product.variants):
            unmatched.append(Miss(block, "no_variants"))
            continue
        old, vat_rate = current(product)
        matched.append(Match(block, product, matched_by, vat_rate or DEFAULT_VAT, old, alias_id))

    order = lambda item: (item.block.sheet, item.block.row, item.block.side)  # noqa: E731
    return Preview(sorted(matched, key=order), sorted(suggested, key=order), sorted(unmatched, key=order))


def new_prices(match: Match) -> dict[SegmentType, tuple[Decimal, Decimal]]:
    """(excl, incl) per group: the sheet's prices are in TL with VAT included."""
    factor = 1 + match.vat_rate / 100
    return {
        seg: ((incl / factor).quantize(CENT), incl)
        for seg, incl in match.block.prices.items()
        if incl is not None
    }


async def apply_matches(db: AsyncSession, preview: Preview) -> int:
    """Write every matched row's four prices to all of its product's colours. The caller commits."""
    segments = await segment_ids(db)
    variants = 0
    for match in preview.matched:
        active = [v for v in match.product.variants if v.is_active]
        rows = [
            (segments[seg], CurrencyType.TRY, excl, incl)
            for seg, (excl, incl) in new_prices(match).items()
            if seg in segments
        ]
        await write_prices(db, active, rows, match.vat_rate)
        variants += len(active)
    return variants
