"""The API's interactive docs and schema: off in production (DEBUG off), on for development."""

import importlib

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings


@pytest.mark.parametrize(("debug", "status"), [(False, 404), (True, 200)])
async def test_docs_only_while_debugging(monkeypatch, debug: bool, status: int):
    monkeypatch.setattr(settings, "DEBUG", debug)
    import app.main as main

    app = importlib.reload(main).app
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for path in ("/docs", "/redoc", "/openapi.json"):
                assert (await client.get(path)).status_code == status, path
    finally:
        monkeypatch.undo()
        importlib.reload(main)
