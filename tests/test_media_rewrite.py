"""Stored local media URLs follow the address the API is reached on."""

import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.media_rewrite import LocalMediaRewriteMiddleware
from app.main import app
from app.models.catalog import Product

LOCAL = "http://127.0.0.1:8000/media/products/opencart/abc.jpg"


async def _image_seen_from(base_url: str, suffix: str, headers: dict | None = None) -> str:
    async with AsyncClient(transport=ASGITransport(app=app), base_url=base_url) as client:
        resp = await client.get("/api/v1/products/", params={"q": suffix, "limit": 5}, headers=headers or {})
    assert resp.status_code == 200, resp.text
    assert int(resp.headers["content-length"]) == len(resp.content)
    return resp.json()[0]["image_url"]


async def test_local_media_urls_follow_the_request_origin(db_session: AsyncSession):
    suffix = uuid.uuid4().hex[:8]
    product = Product(base_code=f"MED-{suffix}", name=f"Medya {suffix}", tags=[], image_url=LOCAL, image_urls=[LOCAL])
    db_session.add(product)
    await db_session.commit()
    try:
        # Through a tunnel: the public address, https, same path.
        assert await _image_seen_from("https://abc.trycloudflare.com", suffix) == (
            "https://abc.trycloudflare.com/media/products/opencart/abc.jpg"
        )
        # On this machine: exactly what is stored.
        assert await _image_seen_from("http://127.0.0.1:8000", suffix) == LOCAL
        assert await _image_seen_from("http://localhost:8000", suffix) == LOCAL
    finally:
        await db_session.execute(delete(Product).where(Product.id == product.id))
        await db_session.commit()


async def test_public_media_url_replaces_every_request_origin():
    """Behind the storefront's proxy the request's own address is the API's, over
    plain http; one public address answers instead, whichever Host came in."""

    async def stored(request):
        return JSONResponse({"image_url": LOCAL})

    proxied = LocalMediaRewriteMiddleware(
        Starlette(routes=[Route("/", stored)]),
        local_origins=["http://127.0.0.1:8000"],
        public_url="https://shop.example/api/backend/media",
    )
    for base_url in ("http://209.38.195.227", "http://209.38.195.227:8000", "http://127.0.0.1:8000"):
        async with AsyncClient(transport=ASGITransport(app=proxied), base_url=base_url) as client:
            resp = await client.get("/")
        assert resp.json()["image_url"] == "https://shop.example/api/backend/media/products/opencart/abc.jpg"
        assert int(resp.headers["content-length"]) == len(resp.content)


async def _preflight(origin: str) -> str | None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        resp = await client.options(
            "/api/v1/products/",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
        )
    return resp.headers.get("access-control-allow-origin")


async def test_cors_answers_configured_origins_only():
    from app.core.config import settings

    allowed = settings.CORS_ORIGINS[0]
    assert await _preflight(allowed) == allowed
    assert await _preflight("https://evil.example.com") is None
