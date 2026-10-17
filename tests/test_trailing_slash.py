"""No request is answered with a slash redirect: both spellings reach the route.

Behind the storefront's proxy a redirect's Location names the API's own
address over http, which an HTTPS page refuses (app/core/trailing_slash.py).
"""

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient

from app.main import app


def test_no_route_is_declared_with_a_trailing_slash():
    slashed = [r.path for r in app.routes if isinstance(r, APIRoute) and r.path != "/" and r.path.endswith("/")]
    assert slashed == []


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/api/v1/products", 200),
        ("/api/v1/categories", 200),
        ("/api/v1/brands", 200),
        # Matched, then refused for want of a login: not a redirect, not a 404.
        ("/api/v1/orders", 401),
        ("/api/v1/no-such-thing", 404),
    ],
)
async def test_both_spellings_are_answered_in_place(client: AsyncClient, path: str, status: int):
    for spelling in (path, f"{path}/"):
        # The Host a proxy forwards, as Vercel's rewrite did: it must never surface.
        resp = await client.get(spelling, params={"limit": 1}, headers={"host": "209.38.195.227"})
        assert resp.status_code == status, (spelling, resp.status_code, resp.headers.get("location"))
        assert "location" not in resp.headers
