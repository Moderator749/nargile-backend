"""Uploaded image storage under MEDIA_ROOT, served at /media."""

import io
import secrets
import uuid
import warnings

from PIL import Image, ImageOps
from starlette.requests import Request

from app.core.config import settings
from app.core.slugs import slugify

MAX_IMAGE_BYTES = 1024 * 1024  # 1 MB: logos are small
MAX_PRODUCT_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB: product photography is not

# A named file's words, before its "-<hash>": enough for a product's code and
# name, short of a path that wraps.
MAX_NAME_CHARS = 80

# Raster only, identified by magic bytes (never the client's filename or
# Content-Type). SVG is refused: it can carry script when opened directly.
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
)


class InvalidImage(ValueError):
    pass


# Photos as the storefront shows them: the longest side at most this (a
# product page's gallery is at most ~900px wide, so this is its retina size),
# and WebP at this quality — a photograph's detail kept, a third of a JPEG's
# bytes. A picture with transparency (a logo, a cut-out) is kept lossless: a
# lossy encoder softens exactly the hard edges it is made of.
MAX_SIDE = 1800
WEBP_QUALITY = 82

# A file claiming more pixels than this is refused before it is decoded (a
# "decompression bomb": a few kilobytes that unpack to gigabytes).
Image.MAX_IMAGE_PIXELS = 50_000_000


def optimise_photo(data: bytes) -> bytes:
    """Any PNG, JPEG or WebP as a WebP for the storefront.

    Turned upright as the camera meant it (EXIF orientation) before the EXIF
    is dropped — the GPS position, the camera, the date go with it; the colour
    profile stays, so the colours do not shift. Shrunk (never enlarged) to
    MAX_SIDE on its longest side, keeping its proportions (Lanczos).
    """
    try:
        with warnings.catch_warnings():
            # Past MAX_IMAGE_PIXELS Pillow only warns (it refuses at twice that): refuse there.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            source = Image.open(io.BytesIO(data))
            source.load()
        with source:
            source.seek(0)  # an animated file: its first frame
            image = ImageOps.exif_transpose(source)
            icc = source.info.get("icc_profile")
            alpha = image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info)
            image = image.convert("RGBA" if alpha else "RGB")
            image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            options: dict = {"lossless": True, "quality": 80} if alpha else {"quality": WEBP_QUALITY}
            image.save(out, "WEBP", method=6, **options, **({"icc_profile": icc} if icc else {}))
            return out.getvalue()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise InvalidImage("Görselin çözünürlüğü çok yüksek") from None
    except (OSError, ValueError, SyntaxError):
        raise InvalidImage("Görsel okunamadı") from None


def image_extension(data: bytes) -> str:
    for magic, ext in _SIGNATURES:
        if data.startswith(magic):
            return ext
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    raise InvalidImage("Only PNG, JPEG or WebP images are accepted")


def media_url(request: Request, rel: str) -> str:
    """The address a browser loads an uploaded file from: PUBLIC_MEDIA_URL when
    set, else the /media route on the address the request came in on."""
    if settings.PUBLIC_MEDIA_URL:
        return f"{settings.PUBLIC_MEDIA_URL.rstrip('/')}/{rel}"
    return str(request.url_for("media", path=rel))


def file_stem(name: str | None) -> str | None:
    """The words a file is named by: "LH013 Legend Hookah Çantalı" → "lh013-legend-hookah-cantali";
    None when nothing nameable is left."""
    stem = slugify(name or "")[:MAX_NAME_CHARS].strip("-")
    return stem or None


def save_image(
    data: bytes,
    folder: str,
    max_bytes: int = MAX_IMAGE_BYTES,
    name: str | None = None,
    optimise: bool = False,
) -> str:
    """Store an image under MEDIA_ROOT/folder; returns its relative path.

    Never under the client's filename (WhatsApp_Image.jpg, IMG_9912.png): with
    a `name` (a product's slug) the file is "<name>-<8 hex>.<ext>", which image
    search reads; without one, a random name. The extension is the content's.
    With `optimise`, the file stored is the photo as a WebP (optimise_photo),
    whatever was sent; `max_bytes` holds for what was sent.
    """
    if not data:
        raise InvalidImage("Empty file")
    if len(data) > max_bytes:
        raise InvalidImage(f"Görsel en fazla {max_bytes // (1024 * 1024)} MB olabilir")
    image_extension(data)  # a PNG, JPEG or WebP, by its bytes, before anything decodes it
    if optimise:
        data = optimise_photo(data)
    ext, stem = image_extension(data), file_stem(name)
    folder_path = settings.MEDIA_ROOT / folder
    folder_path.mkdir(parents=True, exist_ok=True)
    while True:
        filename = f"{stem}-{secrets.token_hex(4)}{ext}" if stem else f"{uuid.uuid4().hex}{ext}"
        try:
            # "x": a name already taken (a photo of the same product, 1 in 4 billion) draws another.
            with open(folder_path / filename, "xb") as f:
                f.write(data)
        except FileExistsError:
            continue
        return f"{folder}/{filename}"


def delete_media(rel: str | None) -> None:
    if not rel:
        return
    root = settings.MEDIA_ROOT.resolve()
    path = (root / rel).resolve()
    if path.is_relative_to(root):  # never follow a stored path outside the media root
        path.unlink(missing_ok=True)


