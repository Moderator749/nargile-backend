""""Fiyat Hesaplama Cetveli": save worksheet rows, keep them in files, and
push their prices to the live product.

- ``POST /admin/pricing/draft``: a row into "Taslaklar" (no file).
- ``POST /admin/pricing/file``: a row into an existing file, or a new one.
- ``POST /admin/pricing/apply-to-product``: the same, then the row's prices
  written to the product its "Ürün Kodu" names.
- ``POST /admin/pricing/import-excel``: the supplier's pricing workbook, every
  block's four prices matched to a product (``dry_run``, the default, only
  reports what would change; suggestions are never priced).
- ``POST /admin/pricing/custom-catalog``: a storefront section's products at
  one price group's prices, for the same printable catalogue a B2B account
  downloads ("PDF Oluştur").
- ``/admin/pricing/import-aliases``: the workbook names the admin has tied to
  products by confirming a suggestion — matched first on every later upload.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile, status
from starlette.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_admin_user, get_session_factory
from app.core.database import get_db
from app.models.catalog import CurrencyType, Product, ProductVariant, VariantPrice
from app.api.v1.products import build_price_list
from app.models.pricing import PriceImportAlias, PricingFile, PricingRecord
from app.models.user import CustomerSegment, SegmentType, User
from app.schemas.catalog import AdminCatalogRequest, PriceListItem
from app.schemas.pricing import (
    CurrentPrice,
    ImportAliasBatch,
    ImportAliasCreate,
    ImportAliasRead,
    ImportCandidate,
    ImportedRow,
    ImportMatched,
    ImportSkipped,
    ImportSuggested,
    ImportUnmatched,
    PriceImportResult,
    DuplicateFileRequest,
    MoveRecordsRequest,
    PricingFileRead,
    PricingRecordRead,
    PricingSheetIn,
    ProductErpCatalog,
    RecordIds,
    RecordsChanged,
    RenameFileRequest,
    SaveApplied,
    SaveRequest,
    SaveResult,
    SaveTarget,
)
from app.services.price_import import ImportFileError, apply_matches, match_blocks, normalize, parse_workbook
from app.services import indexnow
from app.services.pricing_engine import ci
from app.services.pricing_sheet import ApplyError, apply_to_product, compute_sheet
from app.services.product_blueprint import erp_catalog

router = APIRouter(prefix="/admin/pricing", tags=["admin-pricing"])


def _read(
    record: PricingRecord,
    file_name: str | None,
    image_url: str | None = None,
    product_erp: ProductErpCatalog | None = None,
    site_drift: list[str] | None = None,
) -> PricingRecordRead:
    summary = record.applied_summary or {}
    return PricingRecordRead(
        superseded=bool(summary.get("superseded_at")) if record.applied_at else False,
        site_drift=site_drift,
        id=record.id, file_id=record.file_id, file_name=file_name,
        product_code=record.product_code, product_name=record.product_name, image_url=image_url,
        sheet=record.sheet,
        applied_at=record.applied_at, applied_summary=record.applied_summary,
        created_at=record.created_at, updated_at=record.updated_at,
        product_erp=product_erp,
    )


def _write_sheet(record: PricingRecord, sheet: PricingSheetIn) -> None:
    """The row's code, name and worksheet, from what was typed."""
    record.product_code = sheet.product_code
    record.product_name = sheet.product_name
    record.sheet = compute_sheet(sheet)


async def _site_drift(db: AsyncSession, records: list[PricingRecord]) -> dict[uuid.UUID, list[str]]:
    """Per record still on the site (applied, not superseded), the groups whose
    live price is no longer what it applied: changed in the product form, an
    import, a script. Read in one query for the page's records."""
    on_site = {
        r.id: r for r in records
        if r.applied_at and r.applied_summary and r.applied_summary.get("product_id")
        and not r.applied_summary.get("superseded_at")
    }
    if not on_site:
        return {}
    product_ids = {uuid.UUID(r.applied_summary["product_id"]) for r in on_site.values()}
    live: dict[tuple[str, str, str], set[Decimal]] = {}
    rows = await db.execute(
        select(ProductVariant.product_id, CustomerSegment.code, VariantPrice.currency, VariantPrice.price_incl_tax)
        .join(VariantPrice, VariantPrice.variant_id == ProductVariant.id)
        .join(CustomerSegment, CustomerSegment.id == VariantPrice.segment_id)
        .join(Product, Product.id == ProductVariant.product_id)
        .where(ProductVariant.product_id.in_(product_ids), ProductVariant.is_active.is_(True), Product.is_deleted.is_(False))
    )
    for product_id, segment, currency, incl in rows:
        live.setdefault((str(product_id), SegmentType(segment).value, CurrencyType(currency).value), set()).add(incl)
    drift: dict[uuid.UUID, list[str]] = {}
    for record in on_site.values():
        pid = record.applied_summary["product_id"]
        changed = [
            p.get("label") or p["segment"]
            for p in record.applied_summary.get("prices", [])
            if live.get((pid, p["segment"], p["currency"])) != {Decimal(p["price_incl_tax"])}
        ]
        if changed:
            drift[record.id] = changed
    return drift


def _supersede_query(product_id: str, but: uuid.UUID):
    """The product's other applied records not yet marked as replaced."""
    return select(PricingRecord).where(
        PricingRecord.applied_summary["product_id"].astext == product_id,
        PricingRecord.applied_at.is_not(None),
        PricingRecord.id != but,
    )


async def _mark_applied(db: AsyncSession, record: PricingRecord, done, now: datetime) -> None:
    """The record is now the product's prices on the site: its summary, and
    every earlier record applied to the same product marked superseded (kept,
    with when and by which record), so only one shows "Sitede"."""
    product_id = str(done.product.id)
    record.applied_at = now
    record.applied_summary = {
        "product_id": product_id,
        "product_name": done.product.name,
        "variants": done.variants,
        "prices": [p.model_dump(mode="json") for p in done.prices],
        "skipped": done.skipped,
        "cost": str(done.cost),
        "cost_currency": done.cost_currency.value,
    }
    for older in (await db.scalars(_supersede_query(product_id, record.id))).all():
        if not (older.applied_summary or {}).get("superseded_at"):
            older.applied_summary = {
                **older.applied_summary, "superseded_at": now.isoformat(), "superseded_by": str(record.id),
            }


async def _images(db: AsyncSession, codes: set[str]) -> dict[str, str]:
    """Each code's product photo, by product code or a colour's SKU on sale
    (case-insensitive). A deleted product is not looked at: its code may be a
    new product's now, or nobody's."""
    wanted = {ci(c.strip()) for c in codes if c and c.strip()}
    if not wanted:
        return {}
    found: dict[str, str] = {}
    live = Product.is_deleted.is_(False)
    by_sku = await db.execute(
        select(func.lower(ProductVariant.sku), Product.image_url)
        .join(Product, Product.id == ProductVariant.product_id)
        .where(
            func.lower(ProductVariant.sku).in_(wanted), ProductVariant.is_active.is_(True),
            live, Product.image_url.is_not(None),
        )
    )
    found.update({code: url for code, url in by_sku})
    by_code = await db.execute(
        select(func.lower(Product.base_code), Product.image_url)
        .where(func.lower(Product.base_code).in_(wanted), live, Product.image_url.is_not(None))
    )
    found.update({code: url for code, url in by_code})  # the product's own code wins over a SKU
    return found


async def _erp_catalogs(db: AsyncSession, codes: set[str]) -> dict[str, ProductErpCatalog]:
    """Each code's product's "ERP ve Katalog Fiyatları" (the product sheet's),
    by product code or a colour's SKU on sale (case-insensitive), with its
    price table — for the price catalogue. The product's own code wins over a
    SKU. A deleted product is not looked at: a row whose code only a deleted
    product had prices from its own figures, never the deleted one's."""
    wanted = {ci(c.strip()) for c in codes if c and c.strip()}
    if not wanted:
        return {}
    products: dict[str, Product] = {}
    live = Product.is_deleted.is_(False)
    by_sku = (
        select(func.lower(ProductVariant.sku), Product)
        .join(Product, Product.id == ProductVariant.product_id)
        .where(func.lower(ProductVariant.sku).in_(wanted), ProductVariant.is_active.is_(True), live)
    )
    by_code = select(func.lower(Product.base_code), Product).where(func.lower(Product.base_code).in_(wanted), live)
    for stmt in (by_sku, by_code):  # the code after the SKU, so it overwrites it
        for code, product in await db.execute(stmt):
            products[code] = product

    # The price table as the product sheet shows it: its first live colour's
    # (every colour carries the same matrix).
    first: dict[uuid.UUID, uuid.UUID] = {}
    vat_rates: dict[uuid.UUID, Decimal] = {}
    tables: dict[uuid.UUID, dict[CurrencyType, dict[SegmentType, Decimal]]] = {}
    rows = await db.execute(
        select(ProductVariant.product_id, ProductVariant.id, CustomerSegment.code, VariantPrice)
        .join(VariantPrice, VariantPrice.variant_id == ProductVariant.id)
        .join(CustomerSegment, CustomerSegment.id == VariantPrice.segment_id)
        .where(
            ProductVariant.product_id.in_({p.id for p in products.values()}),
            ProductVariant.is_active.is_(True),
        )
        .order_by(ProductVariant.position, ProductVariant.sku)
    )
    for product_id, variant_id, segment, price in rows:
        if first.setdefault(product_id, variant_id) != variant_id:
            continue
        vat_rates.setdefault(product_id, price.vat_rate)
        row = tables.setdefault(product_id, {}).setdefault(CurrencyType(price.currency), {})
        row[SegmentType(segment)] = price.price_excl_tax

    return {
        code: ProductErpCatalog(
            **erp_catalog(product).model_dump(),
            vat_rate=vat_rates.get(product.id),
            prices_excl=tables.get(product.id, {}),
        )
        for code, product in products.items()
    }


async def _image_for(db: AsyncSession, code: str) -> str | None:
    return (await _images(db, {code})).get(ci(code.strip()))


async def _file_for(db: AsyncSession, target: SaveTarget, admin: User) -> PricingFile:
    if target.file_id is not None:
        file = await db.get(PricingFile, target.file_id)
        if file is None:
            raise HTTPException(status_code=404, detail="Dosya bulunamadı")
        return file
    existing = await db.scalar(select(PricingFile).where(func.lower(PricingFile.name) == ci(target.file_name)))
    if existing is not None:
        return existing  # the same name again is the same file
    file = PricingFile(name=target.file_name, created_by=admin.id)
    db.add(file)
    await db.flush()
    return file


def _codes_in_file():
    """The distinct, non-blank product codes of a file's rows, for the file search."""
    return func.array_remove(func.array_agg(PricingRecord.product_code.distinct()), "")


async def _file_read(db: AsyncSession, file: PricingFile) -> PricingFileRead:
    count, codes = (
        await db.execute(select(func.count(), _codes_in_file()).where(PricingRecord.file_id == file.id))
    ).one()
    return PricingFileRead(
        id=file.id,
        name=file.name,
        created_at=file.created_at,
        updated_at=file.updated_at,
        record_count=count or 0,
        product_codes=codes or [],
    )


async def _name_taken(db: AsyncSession, name: str, but: int | None = None) -> bool:
    """Another file already has this name (names are compared without case)."""
    stmt = select(PricingFile.id).where(func.lower(PricingFile.name) == ci(name))
    if but is not None:
        stmt = stmt.where(PricingFile.id != but)
    return await db.scalar(stmt.limit(1)) is not None


@router.get("/files", response_model=list[PricingFileRead])
async def list_files(_admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)):
    # Each file's row count and product codes in one grouped query, so the
    # Dosyalar search can match codes inside files without opening them.
    counts = (
        select(PricingRecord.file_id, func.count().label("n"), _codes_in_file().label("codes"))
        .group_by(PricingRecord.file_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(PricingFile, func.coalesce(counts.c.n, 0), counts.c.codes)
            .outerjoin(counts, counts.c.file_id == PricingFile.id)
            .order_by(PricingFile.updated_at.desc())
        )
    ).all()
    return [
        PricingFileRead(
            id=f.id,
            name=f.name,
            created_at=f.created_at,
            updated_at=f.updated_at,
            record_count=n,
            product_codes=codes or [],
        )
        for f, n, codes in rows
    ]


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(file_id: int, _admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)):
    """The file and its rows. Prices already applied to products stay applied."""
    file = await db.get(PricingFile, file_id)
    if file is None:
        raise HTTPException(status_code=404, detail="Dosya bulunamadı")
    await db.delete(file)
    await db.commit()


@router.patch("/files/{file_id}", response_model=PricingFileRead)
async def rename_file(
    file_id: int,
    payload: RenameFileRequest,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """"Yeniden Adlandır". A name another file has is refused: "Dosyaya kaydet"
    finds a file by its name, so two of the same name could not be told apart."""
    file = await db.get(PricingFile, file_id)
    if file is None:
        raise HTTPException(status_code=404, detail="Dosya bulunamadı")
    if await _name_taken(db, payload.name, but=file.id):
        raise HTTPException(status_code=409, detail=f"“{payload.name}” adında bir dosya zaten var")
    file.name = payload.name
    await db.commit()
    return await _file_read(db, file)


@router.post("/files/{file_id}/duplicate", response_model=PricingFileRead, status_code=status.HTTP_201_CREATED)
async def duplicate_file(
    file_id: int,
    payload: DuplicateFileRequest,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """"Çoğalt": a new file with a copy of every row, to try another scenario
    on. The copies are new rows: none of them has been applied to a product,
    whatever the originals were. They keep the originals' order."""
    source = await db.get(PricingFile, file_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Dosya bulunamadı")
    if payload.name:
        if await _name_taken(db, payload.name):
            raise HTTPException(status_code=409, detail=f"“{payload.name}” adında bir dosya zaten var")
        name = payload.name
    else:
        # "Ekim Zam (kopya)", then "(kopya 2)", "(kopya 3)"…, within the 120 characters a name may have.
        n = 1
        while True:
            suffix = " (kopya)" if n == 1 else f" (kopya {n})"
            name = source.name[: 120 - len(suffix)] + suffix
            if not await _name_taken(db, name):
                break
            n += 1
    copy = PricingFile(name=name, created_by=admin.id)
    db.add(copy)
    await db.flush()
    rows = (await db.scalars(select(PricingRecord).where(PricingRecord.file_id == source.id))).all()
    for row in rows:
        db.add(
            PricingRecord(
                file_id=copy.id,
                product_code=row.product_code,
                product_name=row.product_name,
                sheet=dict(row.sheet),
                created_at=row.created_at,
                updated_at=row.updated_at,
                created_by=admin.id,
            )
        )
    await db.commit()
    return await _file_read(db, copy)


@router.post("/records/delete", response_model=RecordsChanged)
async def delete_records(
    payload: RecordIds, _admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)
):
    """"Seçilenleri Sil": those rows only — never their file. Prices already
    applied to products stay applied."""
    rows = (await db.scalars(select(PricingRecord).where(PricingRecord.id.in_(payload.ids)))).all()
    for row in rows:
        await db.delete(row)
    await db.commit()
    return RecordsChanged(count=len(rows))


@router.post("/records/move", response_model=RecordsChanged)
async def move_records(
    payload: MoveRecordsRequest,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """"Taşı": the picked rows into another file, or a new one by that name.
    They keep their own dates; the file they go to counts as saved to now."""
    target = await _file_for(db, payload, admin)
    rows = (await db.scalars(select(PricingRecord).where(PricingRecord.id.in_(payload.ids)))).all()
    for row in rows:
        row.file_id = target.id
    if rows:
        target.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return RecordsChanged(count=len(rows), file=await _file_read(db, target))


# "Kayıtlar": every row, whatever its folder, newest first.
ALL_RECORDS_LIMIT = 1000


@router.get("/records", response_model=list[PricingRecordRead])
async def list_records(
    file_id: int | None = Query(None, description="A file's rows; omit for the drafts"),
    everything: bool = Query(False, alias="all", description="Every row, in a folder or not (\"Kayıtlar\")"),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(PricingRecord, PricingFile.name).outerjoin(PricingFile, PricingFile.id == PricingRecord.file_id)
    if not everything:
        stmt = stmt.where(PricingRecord.file_id == file_id if file_id is not None else PricingRecord.file_id.is_(None))
    limit = ALL_RECORDS_LIMIT if everything else 500
    rows = (await db.execute(stmt.order_by(PricingRecord.updated_at.desc(), PricingRecord.id).limit(limit))).all()
    codes = {r.product_code for r, _ in rows}
    images, erp = await _images(db, codes), await _erp_catalogs(db, codes)
    drift = await _site_drift(db, [r for r, _ in rows])
    return [
        _read(r, name, images.get(key), erp.get(key), drift.get(r.id))
        for r, name in rows
        for key in [ci(r.product_code.strip())]
    ]


@router.get("/records/matching", response_model=list[PricingRecordRead])
async def matching_records(
    code: str = Query(..., max_length=60, description="A product code, any case"),
    file_id: int | None = Query(None, description="In this file"),
    file_name: str | None = Query(None, max_length=120, description="In the file of this name (any case); none of either: the drafts"),
    everywhere: bool = Query(False, description="In any folder or none: what \"Kaydet\" asks about"),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """The rows a save of `code` would sit beside in its destination — a file,
    or the drafts — newest first: "Mevcut Dosya Bulundu" asks about them. A
    file named but not yet made holds none; an empty code matches nothing."""
    wanted = ci(code.strip())
    if not wanted:
        return []
    if everywhere:
        return [_read(r, name) for r, name in await _same_code(db, wanted)]
    if file_id is None and file_name:
        name = ci(" ".join(file_name.split()))
        file_id = await db.scalar(select(PricingFile.id).where(func.lower(PricingFile.name) == name))
        if file_id is None:
            return []
    where = PricingRecord.file_id == file_id if file_id is not None else PricingRecord.file_id.is_(None)
    rows = (
        await db.execute(
            select(PricingRecord, PricingFile.name)
            .outerjoin(PricingFile, PricingFile.id == PricingRecord.file_id)
            .where(where, func.lower(func.trim(PricingRecord.product_code)) == wanted)
            .order_by(PricingRecord.updated_at.desc(), PricingRecord.id)
        )
    ).all()
    return [_read(r, name) for r, name in rows]


async def _same_code(db: AsyncSession, wanted: str) -> list:
    """Every row of this code (trimmed, any case), in any folder, newest first."""
    return (
        await db.execute(
            select(PricingRecord, PricingFile.name)
            .outerjoin(PricingFile, PricingFile.id == PricingRecord.file_id)
            .where(func.lower(func.trim(PricingRecord.product_code)) == wanted)
            .order_by(PricingRecord.updated_at.desc(), PricingRecord.id)
        )
    ).all()


@router.delete("/records/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_record(
    record_id: uuid.UUID, _admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)
):
    record = await db.get(PricingRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Kayıt bulunamadı")
    await db.delete(record)
    await db.commit()


# The calculator's old save endpoints. They skipped the duplicate-code
# question and the per-code lock that "/save" takes, so they are retired: every
# save, draft, file or apply, goes through "/save".
_RETIRED = "Bu uç kullanımdan kaldırıldı; kayıt ve siteye uygulama için /admin/pricing/save kullanın."


@router.post("/draft", status_code=status.HTTP_410_GONE, include_in_schema=False)
@router.post("/file", status_code=status.HTTP_410_GONE, include_in_schema=False)
@router.post("/apply-to-product", status_code=status.HTTP_410_GONE, include_in_schema=False)
async def retired_save(_admin: User = Depends(get_current_admin_user)):
    raise HTTPException(status_code=status.HTTP_410_GONE, detail=_RETIRED)


@router.post("/save", response_model=SaveResult)
async def save(
    payload: SaveRequest,
    background: BackgroundTasks,
    sessions: async_sessionmaker = Depends(get_session_factory),
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """The calculator's save, whichever button pressed it.

    A sheet loaded from a row (or saved once) and still under that row's code
    is saved over it, without a question, as a document is. A new sheet, or
    one whose code was changed, is a new row — unless that code is already
    saved, in a file or the drafts: then 409, with those rows, and the admin
    chooses "Üstüne Yaz" (`overwrite`) or "Yeni Kayıt" (`new`). Nothing is
    created before that answer, a new file included. The row then goes where
    the button said (`place`). With `apply`, the prices go to the product,
    all or nothing, as before.
    """
    sheet = payload.sheet
    wanted = ci(sheet.product_code.strip())
    # Two saves of one code at once (a double click, two tabs) are taken in
    # turn, so the second sees the first's row and asks.
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"pricing-save:{wanted}"))))

    target: PricingRecord | None = None
    if payload.mode == "overwrite":
        target = await db.get(PricingRecord, payload.overwrite_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Üzerine yazılacak kayıt bulunamadı; silinmiş olabilir.")
    elif payload.mode == "auto":
        tied = await db.get(PricingRecord, payload.record_id) if payload.record_id else None
        if tied is not None and ci(tied.product_code.strip()) == wanted:
            target = tied
        elif wanted:
            same = await _same_code(db, wanted)
            if same:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": f"“{same[0][0].product_code}” kodlu bir kayıt zaten var.",
                        "matches": [_read(r, name).model_dump(mode="json") for r, name in same],
                    },
                )

    now = datetime.now(timezone.utc)
    record = target or PricingRecord(created_by=admin.id, created_at=now, file_id=None)
    _write_sheet(record, sheet)
    record.updated_at = now
    if payload.place == "draft":
        record.file_id = None
    elif payload.place == "file":
        record.file_id = (await _file_for(db, SaveTarget(file_id=payload.file_id, file_name=payload.file_name), admin)).id
    file = await db.get(PricingFile, record.file_id) if record.file_id is not None else None
    if file is not None:
        file.updated_at = now  # its folder was saved into just now
    db.add(record)

    applied: SaveApplied | None = None
    image_url: str | None = None
    if payload.apply:
        try:
            done = await apply_to_product(db, sheet, record.sheet)
        except ApplyError as exc:
            await db.rollback()
            raise HTTPException(status_code=422, detail=str(exc))
        await _mark_applied(db, record, done, now)
        applied = SaveApplied(
            product_id=done.product.id,
            product_name=done.product.name,
            variants_updated=done.variants,
            prices=done.prices,
            skipped=done.skipped,
        )
        image_url = done.product.image_url
    await db.commit()
    if applied is not None:
        # The panel's "Kaydet ve Siteye Uygula": tell IndexNow the product changed.
        background.add_task(indexnow.queue_products, sessions, [applied.product_id])
    if image_url is None:
        image_url = await _image_for(db, record.product_code)
    return SaveResult(
        record=_read(record, file.name if file else None, image_url),
        overwritten=target is not None,
        applied=applied,
    )


MAX_WORKBOOK_BYTES = 10 * 1024 * 1024


def _row(block) -> dict:
    return ImportedRow(
        sheet=block.sheet, row=block.row, side=block.side, section=block.section, name=block.name, prices=block.prices
    ).model_dump()


@router.post("/import-excel", response_model=PriceImportResult)
async def import_excel(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    dry_run: bool = Query(True, description="Only report what would change"),
    sessions: async_sessionmaker = Depends(get_session_factory),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Toplu fiyat: the four group prices of every block in the workbook, written
    to the products their codes name — or, with ``dry_run``, only matched and
    reported (see ``app/services/price_import``). All or nothing."""
    data = await file.read(MAX_WORKBOOK_BYTES + 1)
    if len(data) > MAX_WORKBOOK_BYTES:
        raise HTTPException(status_code=413, detail="Dosya en fazla 10 MB olabilir.")
    # An .xlsx is a zip archive; anything else is refused before it is opened.
    if not data.startswith(b"PK\x03\x04"):
        raise HTTPException(status_code=422, detail="Yalnızca .xlsx biçimindeki Excel dosyaları okunabilir.")
    try:
        parsed = await run_in_threadpool(parse_workbook, data)
    except ImportFileError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    preview = await match_blocks(db, parsed)
    variants = 0
    if not dry_run and preview.matched:
        variants = await apply_matches(db, preview)
        await db.commit()
        background.add_task(indexnow.queue_products, sessions, [m.product.id for m in preview.matched])

    return PriceImportResult(
        dry_run=dry_run,
        matched=[
            ImportMatched(
                **_row(m.block),
                product_id=m.product.id,
                product_code=m.product.base_code,
                product_name=m.product.name,
                matched_by=m.matched_by,
                alias_id=m.alias_id,
                variants=sum(1 for v in m.product.variants if v.is_active),
                old_prices={
                    seg: CurrentPrice(currency=currency, price_incl_tax=price)
                    for seg, (currency, price) in m.old_prices.items()
                },
                image_url=m.product.image_url,
            )
            for m in preview.matched
        ],
        suggested=[
            ImportSuggested(
                **_row(item.block),
                candidates=[
                    ImportCandidate(
                        product_id=c.product.id,
                        code=c.product.base_code,
                        name=c.product.name,
                        score=c.score,
                        basis=c.basis,
                        retail=CurrentPrice(currency=c.retail[0], price_incl_tax=c.retail[1]) if c.retail else None,
                        image_url=c.product.image_url,
                    )
                    for c in item.candidates
                ],
                recommended_id=item.recommended.id if item.recommended else None,
            )
            for item in preview.suggested
        ],
        unmatched=[ImportUnmatched(**_row(miss.block), reason=miss.reason) for miss in preview.unmatched],
        skipped=[ImportSkipped(sheet=s.sheet, row=s.row, side=s.side, name=s.name, reason=s.reason) for s in parsed.skipped],
        updated_products=0 if dry_run else len(preview.matched),
        updated_variants=variants,
    )


def _alias(alias: PriceImportAlias, product: Product) -> ImportAliasRead:
    return ImportAliasRead(
        id=alias.id,
        excel_name=alias.excel_name,
        product_id=product.id,
        product_code=product.base_code,
        product_name=product.name,
        image_url=product.image_url,
        created_at=alias.created_at,
    )


@router.get("/import-aliases", response_model=list[ImportAliasRead])
async def list_import_aliases(_admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)):
    rows = await db.execute(
        select(PriceImportAlias, Product)
        .join(Product, Product.id == PriceImportAlias.product_id)
        .where(Product.is_deleted.is_(False))  # a deleted product's names tie to nothing
        .order_by(PriceImportAlias.excel_name)
    )
    return [_alias(alias, product) for alias, product in rows.all()]


async def _save_aliases(db: AsyncSession, admin: User, entries: list[ImportAliasCreate]) -> list[ImportAliasRead]:
    """Tie each name to its product; a name (as compared) already tied moves to
    the new one. Checked as a whole before anything is written. The caller commits."""
    keys = [normalize(entry.excel_name) for entry in entries]
    if not all(keys):
        raise HTTPException(status_code=422, detail="Excel'deki ad boş olamaz.")
    if len(set(keys)) != len(keys):
        raise HTTPException(status_code=422, detail="Aynı ad birden fazla kez gönderildi.")
    ids = {entry.product_id for entry in entries}
    products = {
        p.id: p
        for p in (await db.scalars(select(Product).where(Product.id.in_(ids), Product.is_deleted.is_(False)))).all()
    }
    if missing := ids - products.keys():
        raise HTTPException(status_code=404, detail=f"Ürün bulunamadı: {', '.join(sorted(map(str, missing)))}")
    existing = {
        a.key: a for a in (await db.scalars(select(PriceImportAlias).where(PriceImportAlias.key.in_(keys)))).all()
    }
    saved: list[tuple[PriceImportAlias, Product]] = []
    for key, entry in zip(keys, entries):
        product = products[entry.product_id]
        alias = existing.get(key)
        if alias is None:
            alias = PriceImportAlias(key=key, excel_name=entry.excel_name.strip(), product_id=product.id, created_by=admin.id)
            db.add(alias)
        else:
            alias.excel_name, alias.product_id, alias.created_by = entry.excel_name.strip(), product.id, admin.id
            alias.created_at = datetime.now(timezone.utc)
        saved.append((alias, product))
    await db.flush()
    return [_alias(alias, product) for alias, product in saved]


@router.post("/import-aliases", response_model=ImportAliasRead, status_code=status.HTTP_201_CREATED)
async def save_import_alias(
    payload: ImportAliasCreate,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """A suggestion confirmed (or a product found by hand): this workbook name
    means this product from now on."""
    [saved] = await _save_aliases(db, admin, [payload])
    await db.commit()
    return saved


@router.post("/import-aliases/batch", response_model=list[ImportAliasRead], status_code=status.HTTP_201_CREATED)
async def save_import_aliases(
    payload: ImportAliasBatch,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """"Güvenli önerileri eşleştir": several at once, all or none."""
    saved = await _save_aliases(db, admin, payload.aliases)
    await db.commit()
    return saved


@router.delete("/import-aliases/{alias_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_import_alias(
    alias_id: int, _admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)
):
    alias = await db.get(PriceImportAlias, alias_id)
    if alias is None:
        raise HTTPException(status_code=404, detail="Eşleştirme bulunamadı.")
    await db.delete(alias)
    await db.commit()


@router.post("/custom-catalog", response_model=list[PriceListItem])
async def custom_catalog(
    payload: AdminCatalogRequest,
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """"PDF Oluştur" on the storefront: the products of the section the admin
    is looking at, at the chosen group's prices — the rows of the same price
    catalogue a B2B account downloads (``/products/price-list``), which the
    storefront prints. Any group: unlike an account, an admin may send a
    customer the price of any list."""
    return await build_price_list(db, payload.product_ids, [payload.segment])
