"""The public reads every storefront page makes may be kept by the CDN for a
minute; the admin's own reads and writes of the same data never are."""

import pytest
from httpx import AsyncClient

from app.core.edge_cache import EDGE_CACHE_CONTROL
from app.models.user import User
from tests.conftest import auth_header

PUBLIC = ["/api/v1/banners/active", "/api/v1/announcements/popup", "/api/v1/settings/parameters"]
ADMIN = ["/api/v1/admin/announcements/popup", "/api/v1/admin/settings/parameters"]


@pytest.mark.parametrize("url", PUBLIC)
async def test_public_reads_carry_a_short_cdn_cache(client: AsyncClient, url: str):
    resp = await client.get(url)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == EDGE_CACHE_CONTROL
    # Browsers keep nothing: s-maxage only, no max-age.
    assert "max-age" not in resp.headers["cache-control"].replace("s-maxage", "")


@pytest.mark.parametrize("url", ADMIN)
async def test_admin_reads_are_never_cached(client: AsyncClient, admin_user: User, url: str):
    resp = await client.get(url, headers=auth_header(admin_user))
    assert resp.status_code == 200
    assert "s-maxage" not in resp.headers.get("cache-control", "")
