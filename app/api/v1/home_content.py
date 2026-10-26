"""Homepage CMS: public read (active items) and admin management of hero
slides, promo boxes and advantages."""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
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
        mobile_bg_position=row.mobile_bg_position, mobile_image=row.mobile_image,
        button_size_desktop=row.button_size_desktop, button_size_mobile=row.button_size_mobile,
        is_active=row.is_active, sort_order=row.sort_order,
    )


def promo_read(row: PromoBox, _request: Request) -> PromoBoxRead:
    return PromoBoxRead(
        id=row.id, eyebrow=row.eyebrow, title=row.title, text=row.text, href=row.href,
        requires=row.requires, widget=row.widget,
        top_card_image_url=row.top_card_image_url, top_card_focus_x=row.top_card_focus_x,
        bottom_card_image_url=row.bottom_card_image_url, bottom_card_focus_x=row.bottom_card_focus_x,
        top_title=row.top_title, bottom_text=row.bottom_text,
        top_shade=row.top_shade, bottom_shade=row.bottom_shade,
        is_active=row.is_active, sort_order=row.sort_order,
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


class RowOrderWrite(BaseModel):
    """Every row of a list, in the order the homepage shows them."""

    ids: list[int] = Field(min_length=1, max_length=200)


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

    # Before "/{row_id}", which would otherwise take "order" for an id.
    @admin.put(f"{path}/order", response_model=list[read_model], name=f"reorder_{path}")
    async def reorder_rows(
        payload: RowOrderWrite,
        request: Request,
        _a: User = Depends(get_current_admin_user),
        db: AsyncSession = Depends(get_db),
    ):
        """Every row, first to last, as dragged in the panel: each gets its place
        (10, 20, 30…). The whole list or nothing — a row added or deleted since
        the panel loaded it would otherwise land somewhere nobody chose."""
        rows = {r.id: r for r in (await db.scalars(select(model))).all()}
        if len(set(payload.ids)) != len(payload.ids) or set(payload.ids) != set(rows):
            raise HTTPException(status_code=409, detail=f"The order must list every {name.lower()} once; reload and try again")
        for place, row_id in enumerate(payload.ids, start=1):
            rows[row_id].sort_order = place * 10
        await db.commit()
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


# ── The promo box: one, edited in place ──────────────────────────────────────


async def _the_promo_box(db: AsyncSession) -> PromoBox | None:
    """The box the homepage shows — the first active one in order — or, with
    none active, the first there is."""
    return await db.scalar(
        select(PromoBox).order_by(PromoBox.is_active.desc(), PromoBox.sort_order, PromoBox.id).limit(1)
    )


@admin.get("/promo-box", response_model=PromoBoxRead | None)
async def get_promo_box(
    request: Request, _a: User = Depends(get_current_admin_user), db: AsyncSession = Depends(get_db)
):
    """"Promo Kutusu": the two cards beside the hero, as one box; null before the first save."""
    row = await _the_promo_box(db)
    return promo_read(row, request) if row else None


@admin.put("/promo-box", response_model=PromoBoxRead)
async def save_promo_box(
    payload: PromoBoxWrite,
    request: Request,
    _a: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Save the box the homepage shows (creating it on the first save). It is
    the only one shown: always on, with no condition, and any other row still
    switched on is switched off — kept, not deleted — so what the admin saves
    is what the homepage shows."""
    row = await _the_promo_box(db)
    if row is None:
        row = PromoBox()
        db.add(row)
    for field, value in payload.model_dump(exclude={"is_active", "sort_order", "requires"}).items():
        setattr(row, field, value)
    row.is_active, row.requires = True, None
    await db.flush()
    await db.execute(update(PromoBox).where(PromoBox.id != row.id, PromoBox.is_active.is_(True)).values(is_active=False))
    await db.commit()
    await db.refresh(row)
    return promo_read(row, request)
