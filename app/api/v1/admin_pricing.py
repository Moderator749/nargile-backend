""""Fiyat Hesaplama Cetveli": save worksheet rows, keep them in files, and
push their prices to the live product.

- ``POST /admin/pricing/draft``: a row into "Taslaklar" (no file).
- ``POST /admin/pricing/file``: a row into an existing file, or a new one.
- ``POST /admin/pricing/apply-to-product``: the same, then the row's prices
  written to the product its "Ürün Kodu" names.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.models.catalog import Product, ProductVariant
from app.models.pricing import PricingFile, PricingRecord
from app.models.user import User
from app.schemas.pricing import (
    ApplyResult,
    PricingFileCreate,
    PricingFileRead,
    PricingRecordRead,
    PricingSheetIn,
    SaveDraftRequest,
    SaveTarget,
    SaveToFileRequest,
)
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
