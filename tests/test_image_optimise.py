"""An uploaded photo is stored as the storefront needs it: a WebP, at most
1800px on its longest side, upright, without its EXIF."""

import io

import pytest
from httpx import AsyncClient
from PIL import Image

from app.core.config import settings
from app.models.user import User
from app.services import media
from tests.conftest import auth_header

ORIENTATION, GPS = 0x0112, 0x8825


def _jpeg(size: tuple[int, int], orientation: int | None = None) -> bytes:
    image = Image.new("RGB", size, (180, 40, 40))
    # A camera's EXIF: where it was taken, and which way up.
    exif = Image.Exif()
    exif[GPS] = {1: "N", 2: (41.0, 0.0, 0.0)}
    if orientation:
        exif[ORIENTATION] = orientation
    out = io.BytesIO()
    image.save(out, "JPEG", quality=95, exif=exif.tobytes())
    return out.getvalue()


def _png_with_alpha(size: tuple[int, int]) -> bytes:
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    image.paste((220, 20, 20, 255), (2, 2, size[0] - 2, size[1] - 2))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


async def _upload(client: AsyncClient, admin: User, data: bytes, filename: str = "photo.jpg"):
    return await client.post(
        "/api/v1/admin/uploads",
        headers=auth_header(admin),
        files=[("files", (filename, data, "image/jpeg"))],
        data={"name": "sultan-nargile"},
    )


async def test_a_large_photo_is_a_smaller_webp_without_its_exif(client: AsyncClient, admin_user: User, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_ROOT", tmp_path)
    original = _jpeg((3200, 2000))
    resp = await _upload(client, admin_user, original)
    assert resp.status_code == 201, resp.text
    [saved] = resp.json()
    assert saved["path"].startswith("products/sultan-nargile-") and saved["path"].endswith(".webp")

    stored = (tmp_path / saved["path"]).read_bytes()
    with Image.open(io.BytesIO(stored)) as image:
        assert image.format == "WEBP"
        # Proportions kept: 3200×2000 → 1800×1125.
        assert image.size == (1800, 1125)
        assert not image.getexif()
    assert len(stored) < len(original)


async def test_a_phone_photo_is_turned_upright_before_its_exif_goes(client: AsyncClient, admin_user: User, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_ROOT", tmp_path)
    # Stored landscape, meant portrait (EXIF orientation 6: rotate 90°).
    [saved] = (await _upload(client, admin_user, _jpeg((1200, 800), orientation=6))).json()
    with Image.open(tmp_path / saved["path"]) as image:
        assert image.size == (800, 1200)
        assert ORIENTATION not in image.getexif()


async def test_a_small_photo_is_never_enlarged(client: AsyncClient, admin_user: User, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_ROOT", tmp_path)
    [saved] = (await _upload(client, admin_user, _jpeg((640, 480)))).json()
    with Image.open(tmp_path / saved["path"]) as image:
        assert image.size == (640, 480)


async def test_a_transparent_image_keeps_its_transparency(client: AsyncClient, admin_user: User, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_ROOT", tmp_path)
    [saved] = (await _upload(client, admin_user, _png_with_alpha((400, 200)), "logo.png")).json()
    with Image.open(tmp_path / saved["path"]) as image:
        assert image.format == "WEBP" and image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 0 and image.getpixel((200, 100)) == (220, 20, 20, 255)


@pytest.mark.parametrize("data", [b"\xff\xd8\xff" + b"\x00" * 64, b"GIF89a" + b"\x00" * 64])
async def test_what_is_not_a_readable_photo_is_refused(client: AsyncClient, admin_user: User, tmp_path, monkeypatch, data):
    monkeypatch.setattr(settings, "MEDIA_ROOT", tmp_path)
    assert (await _upload(client, admin_user, data)).status_code == 422
    assert not (tmp_path / "products").exists() or not any((tmp_path / "products").iterdir())


def test_a_decompression_bomb_is_refused_before_it_unpacks(monkeypatch):
    # A file claiming more pixels than allowed is refused without being decoded in full.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100 * 100)
    with pytest.raises(media.InvalidImage):
        media.optimise_photo(_jpeg((200, 200)))
