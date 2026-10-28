"""scripts/convert_media_to_webp.py: the JPEGs and PNGs from before WebP, converted
and re-pointed — and an address to a removed original sent on to its WebP."""

import importlib.util
import io
import json
import sys
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette
from starlette.routing import Mount

from app.core.config import settings
from app.core.media_files import MediaFiles
from app.models.brand import Brand
from app.models.catalog import Product
from app.models.user import User

pytestmark = pytest.mark.asyncio

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "convert_media_to_webp.py"


def _script():
    spec = importlib.util.spec_from_file_location("convert_media_to_webp", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _image(path: Path, size, fmt: str, alpha: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA" if alpha else "RGB", size, (200, 30, 30, 120) if alpha else (200, 30, 30)).save(path, fmt)


async def test_the_old_photos_become_webp_and_the_database_follows(db_session: AsyncSession, tmp_path, monkeypatch):
    convert = _script()
    monkeypatch.setattr(settings, "MEDIA_ROOT", tmp_path)
    tag = uuid.uuid4().hex[:8]
    photo, second, logo, hero = (
        f"products/sultan-{tag}.jpg",
        f"products/kept-{tag}.jpg",
        f"brands/logo-{tag}.png",
        f"hero-slides/wide-{tag}.jpg",
    )
    _image(tmp_path / photo, (3000, 2000), "JPEG")
    _image(tmp_path / second, (300, 200), "JPEG")
    _image(tmp_path / logo, (300, 100), "PNG", alpha=True)
    _image(tmp_path / hero, (3200, 1600), "JPEG")

    url = f"https://www.nargilemalzemesi.com/api/backend/media/{photo}"
    product = Product(base_code=f"CNV-{tag}", name="Dönüşüm", slug=f"donusum-{tag}", tags=[], image_url=url, image_urls=[url])
    brand = Brand(name=f"Logo {tag}", slug=f"logo-{tag}", search_term=f"logo{tag}", logo_path=logo)
    # Something outside the shop's content naming an original: it is kept.
    keeper = User(email=f"keep-{tag}@test.com", hashed_password="x", full_name=f"see /media/{second}")
    db_session.add_all([product, brand, keeper])
    await db_session.commit()
    pattern = convert._reference_pattern(tmp_path)
    originals = [p for p in convert._originals(tmp_path) if tag in p.name]

    try:
        # 1. A dry run writes nothing.
        mapping, failed, _, _ = convert.convert(tmp_path, originals, apply=False)
        changed = await convert.rewrite(mapping, pattern, apply=False)
        assert not list(tmp_path.rglob("*.webp")) and not failed
        assert changed.get("products.image_url") and changed.get("brands.logo_path")
        await db_session.refresh(product)
        assert product.image_url == url

        # 2. --apply: the WebPs, and the database pointing at them.
        mapping, failed, before, after = convert.convert(tmp_path, originals, apply=True)
        await convert.rewrite(mapping, pattern, apply=True)
        assert not failed and after < before
        await db_session.refresh(product)
        await db_session.refresh(brand)
        assert product.image_url == url.replace(".jpg", ".webp")
        assert product.image_urls == [url.replace(".jpg", ".webp")]
        assert brand.logo_path == logo.replace(".png", ".webp")
        with Image.open(tmp_path / photo.replace(".jpg", ".webp")) as image:
            assert image.format == "WEBP" and max(image.size) == 1800
        with Image.open(tmp_path / hero.replace(".jpg", ".webp")) as image:
            assert image.size == (2560, 1280)
        with Image.open(tmp_path / logo.replace(".png", ".webp")) as image:
            assert image.mode == "RGBA"
        # The originals are still there; a second run changes nothing.
        assert (tmp_path / photo).exists()
        mapping, failed, _, _ = convert.convert(tmp_path, originals, apply=True)
        assert await convert.rewrite(mapping, pattern, apply=True) == {}

        # 3. --delete-originals: each that has its WebP and that nothing names.
        named = await convert.still_named(pattern)
        assert second.lower() in named and photo.lower() not in named
        for path in originals:
            rel = path.relative_to(tmp_path).as_posix()
            if path.with_suffix(".webp").exists() and rel.lower() not in named:
                path.unlink()
        assert not (tmp_path / photo).exists() and (tmp_path / second).exists()
    finally:
        await db_session.execute(delete(Product).where(Product.id == product.id))
        await db_session.execute(delete(Brand).where(Brand.id == brand.id))
        await db_session.execute(delete(User).where(User.id == keeper.id))
        await db_session.commit()
        # The script works through the app's own engine; its pooled connections
        # belong to this test's event loop, so they go with it.
        from app.core.database import engine

        await engine.dispose()


async def test_an_address_to_a_removed_original_is_sent_on_to_its_webp(tmp_path):
    _image(tmp_path / "products" / "x-1a2b3c4d.webp", (10, 10), "WEBP")
    app = Starlette(routes=[Mount("/media", MediaFiles(directory=tmp_path))])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        moved = await client.get("/media/products/x-1a2b3c4d.jpg")
        assert moved.status_code == 301 and moved.headers["location"] == "x-1a2b3c4d.webp"
        assert (await client.get("/media/products/x-1a2b3c4d.webp")).status_code == 200
        # Neither a WebP to go to, nor a WebP asked for that is not there: a 404.
        assert (await client.get("/media/products/nothing.jpg")).status_code == 404
        assert (await client.get("/media/products/nothing.webp")).status_code == 404
