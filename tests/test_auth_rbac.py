"""Tests for authentication and RBAC enforcement."""

import pytest
from httpx import AsyncClient

from app.models.user import User
from tests.conftest import auth_header


# ── Unauthenticated access ───────────────────────────────────────────────────


async def test_admin_endpoints_reject_unauthenticated(client: AsyncClient):
    """All admin endpoints must reject requests without a Bearer token."""
    routes = [
        ("GET", "/api/v1/admin/orders"),
        ("POST", "/api/v1/admin/products"),
    ]
    for method, path in routes:
        resp = await client.request(method, path)
        assert resp.status_code in (401, 403), (
            f"{method} {path} returned {resp.status_code}"
        )


# ── Customer cannot access admin endpoints ───────────────────────────────────


async def test_customer_cannot_access_admin_orders(
    client: AsyncClient, customer_user: User
):
    resp = await client.get(
        "/api/v1/admin/orders", headers=auth_header(customer_user)
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Admin privileges required"


async def test_customer_cannot_create_product(
    client: AsyncClient, customer_user: User
):
    resp = await client.post(
        "/api/v1/admin/products",
        headers=auth_header(customer_user),
        json={"base_code": "X", "name": "X"},
    )
    assert resp.status_code == 403


async def test_customer_cannot_approve_order(
    client: AsyncClient, customer_user: User
):
    fake_id = "00000000-0000-0000-0000-000000000000"
    resp = await client.post(
        f"/api/v1/admin/orders/{fake_id}/approve",
        headers=auth_header(customer_user),
    )
    assert resp.status_code == 403


# ── Admin CAN access admin endpoints ────────────────────────────────────────


async def test_admin_can_list_orders(client: AsyncClient, admin_user: User):
    resp = await client.get(
        "/api/v1/admin/orders", headers=auth_header(admin_user)
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_admin_can_create_product(client: AsyncClient, admin_user: User):
    resp = await client.post(
        "/api/v1/admin/products",
        headers=auth_header(admin_user),
        json={"base_code": "RBAC01", "name": "RBAC Test Product"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["base_code"] == "RBAC01"
    assert data["name"] == "RBAC Test Product"


# ── Invalid / expired token ──────────────────────────────────────────────────


async def test_invalid_token_rejected(client: AsyncClient):
    resp = await client.get(
        "/api/v1/admin/orders",
        headers={"Authorization": "Bearer this.is.garbage"},
    )
    assert resp.status_code == 401
