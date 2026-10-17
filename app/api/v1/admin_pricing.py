""""Fiyat Hesaplama Cetveli": save worksheet rows, keep them in files, and
push their prices to the live product.

- ``POST /admin/pricing/draft``: a row into "Taslaklar" (no file).
- ``POST /admin/pricing/file``: a row into an existing file, or a new one.
- ``POST /admin/pricing/apply-to-product``: the same, then the row's prices
  written to the product its "Ürün Kodu" names.
- ``POST /admin/pricing/import-excel``: the supplier's pricing workbook, every
  block's four prices matched to a product (``dry_run``, the default, only
  reports what would change; suggestions are never priced).
- ``/admin/pricing/import-aliases``: the workbook names the admin has tied to
  products by confirming a suggestion — matched first on every later upload.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from starlette.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.models.catalog import Product, ProductVariant
from app.models.pricing import PriceImportAlias, PricingFile, PricingRecord
from app.models.user import User
from app.schemas.pricing import (
    ApplyResult,
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
    PricingFileCreate,
    PricingFileRead,
    PricingRecordRead,
    PricingSheetIn,
    SaveDraftRequest,
    SaveTarget,
    SaveToFileRequest,
)
from app.services.price_import import ImportFileError, apply_matches, match_blocks, normalize, parse_workbook
from app.services.pricing_sheet import ApplyError, apply_to_product, compute_sheet

router = APIRouter(prefix="/admin/pricing", tags=["admin-pricing"])


def _read(record: PricingRecord, file_name: str | None, image_url: str | None = None) -> PricingRecordRead:
    return PricingRecordRead(
        id=record.id, file_id=record.file_id, file_name=file_name,
        product_code=record.product_code, product_name=record.product_name, image_url=image_url,
        sheet=record.sheet,
        applied_at=record.applied_at, applied_summary=record.applied_summary, created_at=record.created_at,
    )


async def _images(db: AsyncSession, codes: set[str]) -> dict[str, str]:
    """Each code's product photo, by product code or a colour's SKU (case-insensitive)."""
    wanted = {c.strip().lower() for c in codes if c and c.strip()}
    if not wanted:
        return {}
    found: dict[str, str] = {}
    by_sku = await db.execute(
        select(func.lower(ProductVariant.sku), Product.image_url)
        .join(Product, Product.id == ProductVariant.product_id)
        .where(func.lower(ProductVariant.sku).in_(wanted), Product.image_url.is_not(None))
    )
    found.update({code: url for code, url in by_sku})
    by_code = await db.execute(
        select(func.lower(Product.base_code), Product.image_url).where(
            func.lower(Product.base_code).in_(wanted), Product.image_url.is_not(None)
        )
    )
    found.update({code: url for code, url in by_code})  # the product's own code wins over a SKU
    return found


async def _image_for(db: AsyncSession, code: str) -> str | None:
    return (await _images(db, {code})).get(code.strip().lower())


async def _file_for(db: AsyncSession, target: SaveTarget, admin: User) -> PricingFile:
    if target.file_id is not None:
        file = await db.get(PricingFile, target.file_id)
        if file is None:
            raise HTTPException(status_code=404, detail="Dosya bulunamadı")
        return file
    existing = await db.scalar(select(PricingFile).where(func.lower(PricingFile.name) == target.file_name.lower()))
    if existing is not None:
        return existing  # the same name again is the same file
    file = PricingFile(name=target.file_name, created_by=admin.id)
    db.add(file)
    await db.flush()
    return file


def _record(sheet: PricingSheetIn, file: PricingFile | None, admin: User) -> PricingRecord:
    if file is not None:
        file.updated_at = datetime.now(timezone.utc)  # the file was saved to just now
    return PricingRecord(
        file_id=file.id if file else None,
        product_code=sheet.product_code,
        product_name=sheet.product_name,
        sheet=compute_sheet(sheet),
        created_by=admin.id,
    )


@router.get("/files", response_model=list[PricingFileRead])
async def list_files(_admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)):
    counts = (
        select(PricingRecord.file_id, func.count().label("n")).group_by(PricingRecord.file_id).subquery()
    )
    rows = (
        await db.execute(
            select(PricingFile, func.coalesce(counts.c.n, 0))
            .outerjoin(counts, counts.c.file_id == PricingFile.id)
            .order_by(PricingFile.updated_at.desc())
        )
    ).all()
    return [
        PricingFileRead(id=f.id, name=f.name, created_at=f.created_at, updated_at=f.updated_at, record_count=n)
        for f, n in rows
    ]


@router.post("/files", response_model=PricingFileRead, status_code=status.HTTP_201_CREATED)
async def create_file(
    payload: PricingFileCreate,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    file = await _file_for(db, SaveTarget(file_name=payload.name), admin)
    await db.commit()
    return PricingFileRead(id=file.id, name=file.name, created_at=file.created_at, updated_at=file.updated_at)


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(file_id: int, _admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)):
    """The file and its rows. Prices already applied to products stay applied."""
    file = await db.get(PricingFile, file_id)
    if file is None:
        raise HTTPException(status_code=404, detail="Dosya bulunamadı")
    await db.delete(file)
    await db.commit()


@router.get("/records", response_model=list[PricingRecordRead])
async def list_records(
    file_id: int | None = Query(None, description="A file's rows; omit for the drafts"),
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(PricingRecord, PricingFile.name).outerjoin(PricingFile, PricingFile.id == PricingRecord.file_id)
    stmt = stmt.where(PricingRecord.file_id == file_id if file_id is not None else PricingRecord.file_id.is_(None))
    rows = (await db.execute(stmt.order_by(PricingRecord.created_at.desc()).limit(500))).all()
    images = await _images(db, {r.product_code for r, _ in rows})
    return [_read(r, name, images.get(r.product_code.strip().lower())) for r, name in rows]


@router.delete("/records/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_record(
    record_id: uuid.UUID, _admin: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)
):
    record = await db.get(PricingRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Kayıt bulunamadı")
    await db.delete(record)
    await db.commit()


@router.post("/draft", response_model=PricingRecordRead, status_code=status.HTTP_201_CREATED)
async def save_draft(
    payload: SaveDraftRequest,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """"Taslak olarak kaydet": the row joins the drafts, no file needed."""
    record = _record(payload.sheet, None, admin)
    db.add(record)
    await db.commit()
    return _read(record, None, await _image_for(db, record.product_code))


@router.post("/file", response_model=PricingRecordRead, status_code=status.HTTP_201_CREATED)
async def save_to_file(
    payload: SaveToFileRequest,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """"Dosyaya kaydet": into the chosen file, or a new one by that name."""
    file = await _file_for(db, payload, admin)
    record = _record(payload.sheet, file, admin)
    db.add(record)
    await db.commit()
    return _read(record, file.name, await _image_for(db, record.product_code))


@router.post("/apply-to-product", response_model=ApplyResult)
async def save_and_apply(
    payload: SaveToFileRequest,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """"Dosya ve sitede ürüne kaydet": save the row, then write its prices to
    the product its code names. All or nothing: if the product cannot be
    priced, nothing is saved either."""
    if not payload.sheet.product_code:
        raise HTTPException(status_code=422, detail="Siteye kaydetmek için ürün kodu gerekli")
    file = await _file_for(db, payload, admin)
    record = _record(payload.sheet, file, admin)
    try:
        applied = await apply_to_product(db, payload.sheet, record.sheet)
    except ApplyError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    record.applied_at = datetime.now(timezone.utc)
    record.applied_summary = {
        "product_id": str(applied.product.id),
        "product_name": applied.product.name,
        "variants": applied.variants,
        "prices": [p.model_dump(mode="json") for p in applied.prices],
        "skipped": applied.skipped,
    }
    db.add(record)
    await db.commit()
    return ApplyResult(
        record=_read(record, file.name, applied.product.image_url),
        product_id=applied.product.id,
        product_name=applied.product.name,
        variants_updated=applied.variants,
        prices=applied.prices,
        skipped=applied.skipped,
    )


MAX_WORKBOOK_BYTES = 10 * 1024 * 1024


def _row(block) -> dict:
    return ImportedRow(
        sheet=block.sheet, row=block.row, side=block.side, section=block.section, name=block.name, prices=block.prices
    ).model_dump()


@router.post("/import-excel", response_model=PriceImportResult)
async def import_excel(
    file: UploadFile = File(...),
    dry_run: bool = Query(True, description="Only report what would change"),
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
    products = {p.id: p for p in (await db.scalars(select(Product).where(Product.id.in_(ids)))).all()}
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
