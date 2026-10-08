"""colour variants with their own stock, plus the product sheet's SEO fields

Colours stop being labels on the product and become the variant axis:

- product_variants gains `color_name`, `position` and `is_active`, with one row
  per colour per product (uq_variant_product_color). Stock already hangs off the
  variant — one `inventory` row per segment quota — so a colour gets its own
  four quotas without touching that table.
- Existing products keep their single variant. One that already has colour
  labels has its variant named after the *first* label, which leaves all of the
  recorded stock exactly where it is, and gains one variant per remaining label:
  priced the same (a colour is not priced differently today) and opening at zero
  for every quota. Nothing is invented and nothing is lost; the admin shares the
  numbers out from the form.
- The named variants take a "-<RENK>" suffix so the stock codes stay tellable
  apart, which is how the product sheet derives them from here on. Orders, sale
  rows and quotas all reference the variant id, never the SKU, so nothing that
  already exists is disturbed.
- products.meta_title / meta_description: the "Açıklama ve SEO" block of the
  sheet, overriding the title and description the product page derives.

Revision ID: e4b7a9c15d38
Revises: d8a3f6c21b94
Create Date: 2026-10-08

"""
import json
import re
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4b7a9c15d38'
down_revision: Union[str, Sequence[str], None] = 'd8a3f6c21b94'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SKU_MAX = 50
# Kept in step with app.services.product_blueprint.color_slug, and copied rather
# than imported: a migration has to keep running against the app it was written
# for, not the app as it is today.
_TR_ASCII = str.maketrans("ÇĞİIÖŞÜçğıiöşü", "CGIIOSUcgiiosu")


def color_slug(label: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", label.translate(_TR_ASCII).upper()).strip("-")


def _derive_sku(base: str, slug: str, taken: set[str]) -> str:
    """"<base>-<RENK>", then -2, -3… until it is free. Falls back to a random
    suffix, so a colour always gets a variant even on a wild collision."""
    for attempt in range(1, 100):
        tail = f"-{slug}" if attempt == 1 else f"-{slug}-{attempt}"
        candidate = f"{base[: SKU_MAX - len(tail)]}{tail}"
        if candidate and candidate not in taken:
            return candidate
    return f"{base[:SKU_MAX - 7]}-{uuid.uuid4().hex[:6].upper()}"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("products", sa.Column("meta_title", sa.String(length=70), nullable=True))
    op.add_column("products", sa.Column("meta_description", sa.String(length=160), nullable=True))

    op.add_column("product_variants", sa.Column("color_name", sa.String(length=40), nullable=True))
    op.add_column(
        "product_variants", sa.Column("position", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "product_variants", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true())
    )
    op.create_index("ix_product_variants_product", "product_variants", ["product_id"])

    _split_existing_colours(op.get_bind())

    # Declared only once the data satisfies it, so a half-migrated table is not a
    # possible outcome: a clash would have failed the inserts above instead.
    op.create_unique_constraint(
        "uq_variant_product_color", "product_variants", ["product_id", "color_name"]
    )


def _split_existing_colours(conn: sa.Connection) -> None:
    """Give every colour label of every product its own variant."""
    taken = set(conn.scalars(sa.text("SELECT sku FROM product_variants")))
    products = conn.execute(
        sa.text(
            """
            SELECT id, base_code, color_names
            FROM products
            WHERE jsonb_typeof(color_names) = 'array' AND jsonb_array_length(color_names) > 0
            ORDER BY id
            """
        )
    ).all()

    for product_id, base_code, color_names in products:
        # The same trimming and case-insensitive de-duplication the sheet applies,
        # so the migrated rows match what a save would have written.
        labels: list[str] = []
        for raw in color_names:
            label = " ".join(str(raw).split())[:40]
            if label and label.casefold() not in {s.casefold() for s in labels}:
                labels.append(label)
        if not labels:
            continue

        source = conn.execute(
            sa.text(
                """
                SELECT id, sku
                FROM product_variants
                WHERE product_id = :pid
                ORDER BY sku
                LIMIT 1
                """
            ),
            {"pid": product_id},
        ).first()
        if source is None:  # a product with no variant at all: nothing to split
            continue

        base = base_code or source.sku
        # A label with nothing codeable in it cannot be a stock code, so it cannot
        # be a colour either; drop it from the product's own list as well, so the
        # list and the variants say the same thing from the first read.
        codeable = [label for label in labels if color_slug(label)]
        if codeable != list(color_names):
            conn.execute(
                sa.text("UPDATE products SET color_names = CAST(:labels AS jsonb) WHERE id = :pid"),
                {"labels": json.dumps(codeable), "pid": product_id},
            )

        for position, label in enumerate(codeable):
            slug = color_slug(label)
            if position == 0:
                # The product's own variant becomes its first colour, keeping the
                # stock, prices and sale row it already has.
                sku = _derive_sku(base, slug, taken - {source.sku})
                taken.discard(source.sku)
                taken.add(sku)
                conn.execute(
                    sa.text(
                        "UPDATE product_variants SET color_name = :label, position = 0, sku = :sku "
                        "WHERE id = :vid"
                    ),
                    {"label": label, "sku": sku, "vid": source.id},
                )
                continue

            sku = _derive_sku(base, slug, taken)
            taken.add(sku)
            variant_id = uuid.uuid4()
            conn.execute(
                sa.text(
                    """
                    INSERT INTO product_variants
                        (id, product_id, sku, barcode, color_name, position, is_active,
                         attributes, cost_price, weight_kg)
                    SELECT :id, :pid, :sku, NULL, :label, :position, true,
                           attributes, cost_price, weight_kg
                    FROM product_variants WHERE id = :source_id
                    """
                ),
                {
                    "id": variant_id,
                    "pid": product_id,
                    "sku": sku,
                    "label": label,
                    "position": position,
                    # A barcode identifies one article, so it is never copied.
                    "source_id": source.id,
                },
            )
            # The price matrix is the product's, so every colour carries it.
            conn.execute(
                sa.text(
                    """
                    INSERT INTO variant_prices
                        (variant_id, segment_id, currency, price_excl_tax, price_incl_tax, vat_rate)
                    SELECT :new_id, segment_id, currency, price_excl_tax, price_incl_tax, vat_rate
                    FROM variant_prices WHERE variant_id = :source_id
                    """
                ),
                {"new_id": variant_id, "source_id": source.id},
            )


def downgrade() -> None:
    """Downgrade schema."""
    # The extra colour variants are the ones at position > 0; their prices, quotas
    # and sale rows go with them. Position 0 kept the product's stock, so what is
    # left is the single variant the product had before.
    for table in ("variant_prices", "inventory", "flash_sales"):
        op.execute(
            f"DELETE FROM {table} WHERE variant_id IN "
            "(SELECT id FROM product_variants WHERE position > 0)"
        )
    # A colour that has been ordered has to stay: its order lines point at it.
    op.execute(
        """
        DELETE FROM product_variants
        WHERE position > 0 AND id NOT IN (SELECT variant_id FROM order_items)
        """
    )

    op.drop_constraint("uq_variant_product_color", "product_variants", type_="unique")
    op.drop_index("ix_product_variants_product", table_name="product_variants")
    op.drop_column("product_variants", "is_active")
    op.drop_column("product_variants", "position")
    op.drop_column("product_variants", "color_name")

    op.drop_column("products", "meta_description")
    op.drop_column("products", "meta_title")
