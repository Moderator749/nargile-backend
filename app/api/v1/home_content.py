"""Homepage CMS: public read (active items) and admin management of hero
slides, promo boxes and advantages."""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.models.content import HeroSlide, PromoBox
from app.models.user import User
from app.schemas.content import (
    HeroSlideRead,
    HeroSlideWrite,
    HomeContentRead,
    PromoBoxRead,
    PromoBoxWrite,
)
from app.services.media import MAX_IMAGE_BYTES, InvalidImage, delete_media, media_url, save_image

router = APIRouter(tags=["home-content"])
admin = APIRouter(prefix="/admin", tags=["admin-home-content"])


def _image(row: HeroSlide, request: Request) -> str | None:
    if row.image_path:
        return media_url(request, row.image_path)
    return row.image_url


def slide_read(row: HeroSlide, request: Request) -> HeroSlideRead:
    return HeroSlideRead(
        id=row.id, eyebrow=row.eyebrow, title=row.title, subtitle=row.subtitle,
        cta_label=row.cta_label, cta_href=row.cta_href,
        guest_cta_label=row.guest_cta_label, guest_cta_href=row.guest_cta_href,
        image=_image(row, request), image_url=row.image_url, has_upload=bool(row.image_path),
        requires=row.requires, text_align=row.text_align, image_focus_x=row.image_focus_x,
        image_focus_y=row.image_focus_y, image_zoom=row.image_zoom, layout=row.layout,
        enable_overlay=row.enable_overlay, font_sizes=row.font_sizes, text_colors=row.text_colors,
        mobile_layout=row.mobile_layout, mobile_font_sizes=row.mobile_font_sizes,
        mobile_bg_position=row.mobile_bg_position,
        is_active=row.is_active, sort_order=row.sort_order,
    )


def promo_read(row: PromoBox, _request: Request) -> PromoBoxRead:
    return PromoBoxRead(
        id=row.id, eyebrow=row.eyebrow, title=row.title, text=row.text, href=row.href,
        requires=row.requires, widget=row.widget, is_active=row.is_active, sort_order=row.sort_order,
    )



# ── Public ───────────────────────────────────────────────────────────────────


@router.get("/home/content", response_model=HomeContentRead)
async def home_content(request: Request, db: AsyncSession = Depends(get_db)):
    async def active(model):
        result = await db.execute(
            select(model).where(model.is_active.is_(True)).order_by(model.sort_order, model.id)
        )
        return result.scalars().all()

    return HomeContentRead(
        slides=[slide_read(r, request) for r in await active(HeroSlide)],
        promos=[promo_read(r, request) for r in await active(PromoBox)],
    )


# ── Admin CRUD (same shape for all three) ────────────────────────────────────


def _register(
    path: str,
    model: Any,
    write: type[BaseModel],
    read_model: type[BaseModel],
    read: Callable[[Any, Request], BaseModel],
    name: str,
    with_image: bool = False,
) -> None:
    async def get_row(db: AsyncSession, row_id: int):
        row = await db.get(model, row_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"{name} not found")
        return row

    @admin.get(path, response_model=list[read_model], name=f"list_{path}")
    async def list_rows(request: Request, _a: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)):
        result = await db.execute(select(model).order_by(model.sort_order, model.id))
        return [read(r, request) for r in result.scalars()]

    @admin.post(path, response_model=read_model, status_code=status.HTTP_201_CREATED, name=f"create_{path}")
    async def create_row(
        payload: write,  # type: ignore[valid-type]
        request: Request,
        _a: User = Depends(get_current_admin_user),
        db: AsyncSession = Depends(get_db),
    ):
        row = model(**payload.model_dump())
        db.add(row)
        await db.commit()
        return read(row, request)

    @admin.put(f"{path}/{{row_id}}", response_model=read_model, name=f"update_{path}")
    async def update_row(
        row_id: int,
        payload: write,  # type: ignore[valid-type]
        request: Request,
        _a: User = Depends(get_current_admin_user),
        db: AsyncSession = Depends(get_db),
    ):
        row = await get_row(db, row_id)
        for field, value in payload.model_dump().items():
            setattr(row, field, value)
        await db.commit()
        return read(row, request)

    @admin.delete(f"{path}/{{row_id}}", status_code=status.HTTP_204_NO_CONTENT, name=f"delete_{path}")
    async def delete_row(row_id: int, _a: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)):
        row = await get_row(db, row_id)
        old = getattr(row, "image_path", None)
        await db.delete(row)
        await db.commit()
        delete_media(old)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if not with_image:
        return

    @admin.post(f"{path}/{{row_id}}/image", response_model=read_model, name=f"upload_{path}")
    async def upload_image(
        row_id: int,
        request: Request,
        file: UploadFile = File(...),
        _a: User = Depends(get_current_admin_user),
        db: AsyncSession = Depends(get_db),
    ):
        """PNG, JPEG or WebP up to 1 MB; replaces the previous upload (wins over image_url)."""
        row = await get_row(db, row_id)
        try:
            stored = save_image(await file.read(MAX_IMAGE_BYTES + 1), path.strip("/"))
        except InvalidImage as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        old, row.image_path = row.image_path, stored
        await db.commit()
        delete_media(old)
        return read(row, request)

    @admin.delete(f"{path}/{{row_id}}/image", response_model=read_model, name=f"remove_image_{path}")
    async def remove_image(
        row_id: int, request: Request, _a: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)
    ):
        row = await get_row(db, row_id)
        old, row.image_path = row.image_path, None
        await db.commit()
        delete_media(old)
        return read(row, request)


_register("/hero-slides", HeroSlide, HeroSlideWrite, HeroSlideRead, slide_read, "Slide", with_image=True)
_register("/promo-boxes", PromoBox, PromoBoxWrite, PromoBoxRead, promo_read, "Promo box")
