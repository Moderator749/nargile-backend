"""A product photo's file is named for the product, never for the client's file.

`POST /admin/uploads` with a `name` (the product's slug, from the form) stores
"<slug>-<8 hex>.<ext>" — the words image search reads, a hash so two photos of
one product never meet — and the URL a form saves points at that file. The
client's filename ("WhatsApp Image….jpeg", "IMG_9912.png") is never used, and
the extension is the content's, not the filename's — and an upload is always
stored as a WebP (tests/test_image_optimise.py).
"""

import io
import re

import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.models.user import User
from app.services import media
from tests.conftest import auth_header

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64


def _photo(fmt: str) -> bytes:
    """A real, tiny image in `fmt`: an upload is decoded now, so magic bytes alone no longer do."""
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(out, fmt)
    return out.getvalue()


async def _upload(client: AsyncClient, admin: User, files, name: str | None = None) -> list[dict]:
    resp = await client.post(
        "/api/v1/admin/uploads",
        headers=auth_header(admin),
        files=[("files", f) for f in files],
        data={"name": name} if name is not None else None,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.parametrize(
    ("name", "stem"),
    [
        ("lh013-legend-hookah-cantali-celik-nargile-takimi", "lh013-legend-hookah-cantali-celik-nargile-takimi"),
        # A name (a new product's slug not yet set) is slugified, Turkish letters and all.
        ("LH013 Legend Hookah Çantalı Çelik Nargile Takımı", "lh013-legend-hookah-cantali-celik-nargile-takimi"),
        # An old store's slug keeps its words; the dot goes, as a path has no use for it.
        ("mr.wood-nargile-takimi", "mr-wood-nargile-takimi"),
        ("../../etc/passwd", "etc-passwd"),
    ],
)
async def test_a_product_photo_is_named_for_the_product(
    client: AsyncClient, admin_user: User, tmp_path, monkeypatch, name, stem
):
    monkeypatch.setattr(settings, "MEDIA_ROOT", tmp_path)
    [saved] = await _upload(client, admin_user, [("WhatsApp Image 2026-10-26 at 14.02.11.jpeg", _photo("PNG"), "image/jpeg")], name)

    assert re.fullmatch(rf"products/{re.escape(stem)}-[0-9a-f]{{8}}\.webp", saved["path"]), saved["path"]
    assert saved["url"].endswith(f"/{saved['path']}")
    assert media.image_extension((tmp_path / saved["path"]).read_bytes()) == ".webp"
    assert [p.name for p in (tmp_path / "products").iterdir()] == [saved["path"].removeprefix("products/")]


async def test_photos_of_one_product_each_get_their_own_file(client: AsyncClient, admin_user: User, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_ROOT", tmp_path)
    saved = await _upload(
        client, admin_user,
        [("IMG_9912.png", _photo("PNG"), "image/png"), ("IMG_9912.png", _photo("JPEG"), "image/png"), ("x.gif", _photo("WEBP"), "image/gif")],
        "sultan-nargile",
    )
    paths = [s["path"] for s in saved]
    assert len(set(paths)) == 3
    # In the order given, each stored as a WebP whatever it came as.
    assert [p.rsplit(".", 1)[1] for p in paths] == ["webp", "webp", "webp"]
    assert all(p.startswith("products/sultan-nargile-") and "IMG" not in p for p in paths)


async def test_a_taken_name_draws_another_hash(admin_user: User, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_ROOT", tmp_path)
    draws = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(media.secrets, "token_hex", lambda n: next(draws))
    first = media.save_image(PNG, "products", name="sultan")
    second = media.save_image(PNG + b"2", "products", name="sultan")
    assert (first, second) == ("products/sultan-aaaaaaaa.png", "products/sultan-bbbbbbbb.png")
    assert (tmp_path / first).read_bytes() == PNG  # not overwritten


@pytest.mark.parametrize("name", [None, "", "  ", "!!!"])
async def test_without_a_name_the_file_is_random_not_the_clients(
    client: AsyncClient, admin_user: User, tmp_path, monkeypatch, name
):
    monkeypatch.setattr(settings, "MEDIA_ROOT", tmp_path)
    [saved] = await _upload(client, admin_user, [("WhatsApp_Image.jpg", _photo("JPEG"), "image/jpeg")], name)
    assert re.fullmatch(r"products/[0-9a-f]{32}\.webp", saved["path"]), saved["path"]


async def test_a_long_name_is_cut_short(client: AsyncClient, admin_user: User, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_ROOT", tmp_path)
    [saved] = await _upload(client, admin_user, [("p.png", _photo("PNG"), "image/png")], "nargile-" * 30)
    stem = saved["path"].removeprefix("products/").rsplit("-", 1)[0]
    assert len(stem) <= media.MAX_NAME_CHARS and not stem.endswith("-")
