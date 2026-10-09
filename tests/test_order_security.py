"""Regression tests for order/checkout authorization (IDOR) and input hardening."""

import uuid

import pytest
from httpx import AsyncClient

from app.models.catalog import ProductVariant
from app.models.user import User
from tests.conftest import ADDRESS, CONSENTS, auth_header


def _body(variant: ProductVariant, quantity: int = 1, **extra) -> dict:
    return {
        **CONSENTS,
        "payment_method": "CREDIT_CARD",
        "items": [{"variant_id": str(variant.id), "quantity": quantity}],
        "shipping_address": ADDRESS,
        **extra,
    }


# ── Authentication required ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/v1/orders/calculate-cart"),
        ("POST", "/api/v1/orders/checkout"),
        ("GET", "/api/v1/orders/"),
        ("GET", f"/api/v1/orders/{uuid.uuid4()}"),
    ],
)
async def test_order_endpoints_require_auth(client: AsyncClient, method: str, path: str):
    resp = await client.request(method, path, json={**CONSENTS, })
    assert resp.status_code in (401, 403)


# ── IDOR: the customer is always the token's user ────────────────────────────


async def test_checkout_ignores_user_id_in_body(
    client: AsyncClient, catalog, customer_user: User, admin_user: User
):
    normal_v, _ = catalog
    resp = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(customer_user),
        json=_body(normal_v, user_id=str(admin_user.id)),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["user_id"] == str(customer_user.id)


async def test_orders_are_scoped_to_owner(
    client: AsyncClient, catalog, customer_user: User, admin_user: User
):
    normal_v, _ = catalog
    created = await client.post(
        "/api/v1/orders/checkout", headers=auth_header(customer_user), json=_body(normal_v)
    )
    assert created.status_code == 201
    order_id = created.json()["id"]

    own = await client.get(f"/api/v1/orders/{order_id}", headers=auth_header(customer_user))
    assert own.status_code == 200

    # Another user (even an admin, on the customer-facing route) cannot read it.
    other = await client.get(f"/api/v1/orders/{order_id}", headers=auth_header(admin_user))
    assert other.status_code == 404

    listing = await client.get("/api/v1/orders/", headers=auth_header(admin_user))
    assert order_id not in {o["id"] for o in listing.json()}
    mine = await client.get("/api/v1/orders/", headers=auth_header(customer_user))
    assert {o["user_id"] for o in mine.json()} == {str(customer_user.id)}


# ── Input hardening ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("quantity", [0, -5])
async def test_non_positive_quantity_rejected(
    client: AsyncClient, catalog, customer_user: User, quantity: int
):
    normal_v, _ = catalog
    resp = await client.post(
        "/api/v1/orders/checkout",
        headers=auth_header(customer_user),
        json=_body(normal_v, quantity=quantity),
    )
    assert resp.status_code == 422


async def test_negative_points_rejected(client: AsyncClient, catalog, customer_user: User):
    normal_v, _ = catalog
    resp = await client.post(
        "/api/v1/orders/calculate-cart",
        headers=auth_header(customer_user),
        json=_body(normal_v, points_to_redeem=-100),
    )
    assert resp.status_code == 422


async def test_empty_cart_rejected(client: AsyncClient, customer_user: User):
    resp = await client.post(
        "/api/v1/orders/calculate-cart",
        headers=auth_header(customer_user),
        json={"payment_method": "CREDIT_CARD", "items": []},
    )
    assert resp.status_code == 422


async def test_duplicate_lines_are_summed(client: AsyncClient, catalog, customer_user: User):
    normal_v, _ = catalog
    line = {"variant_id": str(normal_v.id), "quantity": 1}
    resp = await client.post(
        "/api/v1/orders/calculate-cart",
        headers=auth_header(customer_user),
        json={"payment_method": "CREDIT_CARD", "items": [line, line]},
    )
    assert resp.status_code == 200
    assert resp.json()["items"][0]["quantity"] == 2


# ── Public catalog is read-only ──────────────────────────────────────────────


async def test_public_product_writes_removed(client: AsyncClient, customer_user: User):
    create = await client.post(
        "/api/v1/products/",
        headers=auth_header(customer_user),
        json={"base_code": "HACK", "name": "Injected"},
    )
    assert create.status_code == 405
    variant = await client.post(
        f"/api/v1/products/{uuid.uuid4()}/variants",
        headers=auth_header(customer_user),
        json={"sku": "HACK-1"},
    )
    assert variant.status_code in (404, 405)


# ── /auth/me exposes the ParaPuan balance ────────────────────────────────────


async def test_me_includes_loyalty_balance(client: AsyncClient, customer_user: User):
    resp = await client.get("/api/v1/auth/me", headers=auth_header(customer_user))
    assert resp.status_code == 200
    assert resp.json()["loyalty_balance"] == 0
