"""Remove rows the test suite leaked into the dev database (before tests had
their own database; see tests/conftest.py).

Matches only what the fixtures generate: users "<prefix>-<8 hex>@test.com" and
products "<PREFIX>-<6 hex>" (NRM, HVY, LP, SLA, CAT, FLT, OUT, PCT) and the RBAC
test product, plus their
orders, prices, stock, ledgers and flash sales. One transaction; aborts if a
real customer's order contains a test product.

    python -m scripts.purge_test_data          # dry run: counts only
    python -m scripts.purge_test_data --yes    # delete
"""

import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings

TEST_USERS = r"email ~ '^(admin|customer|dealer|loyal|reg|sla)-[0-9a-f]{8}@test\.com$'"
# test_auth_rbac.py creates "RBAC Test Product" (RBAC01) through the admin API.
TEST_PRODUCTS = (
    r"(base_code ~ '^(NRM|HVY|LP|SLA|CAT|FLT|OUT|PCT)-[0-9a-f]{6}$'"
    r" OR (base_code = 'RBAC01' AND name = 'RBAC Test Product'))"
)

SETUP = f"""
CREATE TEMP TABLE purge_users ON COMMIT DROP AS SELECT id FROM users WHERE {TEST_USERS};
CREATE TEMP TABLE purge_products ON COMMIT DROP AS SELECT id FROM products WHERE {TEST_PRODUCTS};
CREATE TEMP TABLE purge_variants ON COMMIT DROP AS
    SELECT id FROM product_variants WHERE product_id IN (SELECT id FROM purge_products);
CREATE TEMP TABLE purge_orders ON COMMIT DROP AS
    SELECT id FROM orders WHERE user_id IN (SELECT id FROM purge_users)
    UNION SELECT order_id FROM order_items WHERE variant_id IN (SELECT id FROM purge_variants);
"""

# Children first.
DELETES = [
    ("loyalty_ledger", "DELETE FROM loyalty_ledger WHERE user_id IN (SELECT id FROM purge_users) OR order_id IN (SELECT id FROM purge_orders)"),
    ("order_items", "DELETE FROM order_items WHERE order_id IN (SELECT id FROM purge_orders)"),
    ("orders", "DELETE FROM orders WHERE id IN (SELECT id FROM purge_orders)"),
    ("company_profiles", "DELETE FROM company_profiles WHERE user_id IN (SELECT id FROM purge_users)"),
    ("site_announcements.updated_by", "UPDATE site_announcements SET updated_by = NULL WHERE updated_by IN (SELECT id FROM purge_users)"),
    ("flash_sales", "DELETE FROM flash_sales WHERE variant_id IN (SELECT id FROM purge_variants)"),
    ("variant_prices", "DELETE FROM variant_prices WHERE variant_id IN (SELECT id FROM purge_variants)"),
    ("inventory", "DELETE FROM inventory WHERE variant_id IN (SELECT id FROM purge_variants)"),
    ("product_variants", "DELETE FROM product_variants WHERE id IN (SELECT id FROM purge_variants)"),
    ("products", "DELETE FROM products WHERE id IN (SELECT id FROM purge_products)"),
    ("users", "DELETE FROM users WHERE id IN (SELECT id FROM purge_users)"),
]


async def main(apply: bool) -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    async with engine.begin() as conn:
        for statement in SETUP.strip().split(";"):
            if statement.strip():
                await conn.execute(text(statement))

        real = await conn.scalar(text(
            "SELECT count(*) FROM orders WHERE id IN (SELECT id FROM purge_orders) "
            "AND user_id NOT IN (SELECT id FROM purge_users)"
        ))
        if real:
            raise SystemExit(f"Aborting: {real} real customer order(s) contain test products.")

        for table, sql in DELETES:
            result = await conn.execute(text(sql))
            print(f"  {table:<30} {result.rowcount}")

        if not apply:
            await conn.rollback()
            print("Dry run: nothing deleted. Re-run with --yes to apply.")
        else:
            print("Deleted.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(apply="--yes" in sys.argv))
