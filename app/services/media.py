"""Uploaded image storage under MEDIA_ROOT, served at /media."""

import uuid
from pathlib import Path

from app.core.config import settings

MAX_IMAGE_BYTES = 1024 * 1024  # 1 MB: logos are small
MAX_PRODUCT_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB: product photography is not

# Raster only, identified by magic bytes (never the client's filename or
# Content-Type). SVG is refused: it can carry script when opened directly.
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
)


class InvalidImage(ValueError):
    pass


def image_extension(data: bytes) -> str:
    for magic, ext in _SIGNATURES:
        if data.startswith(magic):
            return ext
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    raise InvalidImage("Only PNG, JPEG or WebP images are accepted")


def save_image(data: bytes, folder: str, max_bytes: int = MAX_IMAGE_BYTES) -> str:
    """Store an image under MEDIA_ROOT/folder with a random name; returns its relative path."""
    if not data:
        raise InvalidImage("Empty file")
    if len(data) > max_bytes:
        raise InvalidImage(f"Görsel en fazla {max_bytes // (1024 * 1024)} MB olabilir")
    rel = f"{folder}/{uuid.uuid4().hex}{image_extension(data)}"
    target = settings.MEDIA_ROOT / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return rel


def delete_media(rel: str | None) -> None:
    if not rel:
        return
    root = settings.MEDIA_ROOT.resolve()
    path = (root / rel).resolve()
    if path.is_relative_to(root):  # never follow a stored path outside the media root
        path.unlink(missing_ok=True)


def media_path(rel: str) -> Path:
    return settings.MEDIA_ROOT / rel
