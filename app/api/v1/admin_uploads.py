"""Admin image uploads, for any form that needs a URL to store.

Files land under MEDIA_ROOT (served at /media) through the same checked pipeline
as brand logos: magic-byte sniffing, never the client's filename or Content-Type.
A product's photos are named for it ("lh013-legend-hookah-3f9a1c2e.webp", from
the `name` the form sends: its slug), for image search; the response gives the
absolute URL a form can save straight into its record.
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel

from app.api.deps import get_current_admin_user
from app.models.user import User
from app.services.media import MAX_PRODUCT_IMAGE_BYTES, InvalidImage, media_url, save_image

router = APIRouter(prefix="/admin", tags=["admin-uploads"])

MAX_FILES_PER_REQUEST = 12


class UploadedImage(BaseModel):
    """Where the file now lives: `url` to store, `path` to delete it later."""

    url: str
    path: str


@router.post("/uploads", response_model=list[UploadedImage], status_code=status.HTTP_201_CREATED)
async def upload_images(
    request: Request,
    files: list[UploadFile] = File(...),
    # What the photos show, as the product's slug or its name; slugified here.
    name: str | None = Form(default=None, max_length=300),
    _admin: User = Depends(get_current_admin_user),
):
    """PNG, JPEG or WebP up to 5 MB each; several at once, order preserved,
    each named "<name>-<hash>" (a random name without `name`)."""
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(status_code=422, detail=f"En fazla {MAX_FILES_PER_REQUEST} görsel yükleyebilirsiniz")

    saved: list[UploadedImage] = []
    for file in files:
        data = await file.read(MAX_PRODUCT_IMAGE_BYTES + 1)
        try:
            path = save_image(data, "products", MAX_PRODUCT_IMAGE_BYTES, name=name)
        except InvalidImage as exc:
            name = (file.filename or "görsel").rsplit("/", 1)[-1][:60]
            raise HTTPException(status_code=422, detail=f"{name}: {exc}") from None
        saved.append(UploadedImage(url=media_url(request, path), path=path))
    return saved
