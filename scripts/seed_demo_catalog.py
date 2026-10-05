"""Seed a realistic Nargile/Hookah demo catalog.

Populates categories, products, variants with 4-segment pricing matrices,
and dual-warehouse inventory (RETAIL_STOCK + B2B_STOCK).

Idempotent: skips items whose SKU or slug already exists.
"""

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.core.database import async_session
from app.models.catalog import (
    Category,
    CurrencyType,
    Inventory,
    Product,
    ProductVariant,
    VariantPrice,
    WarehouseType,
)
from app.models.user import CustomerSegment, SegmentType

VAT_RATE = Decimal("18.00")
VAT_MULT = Decimal("1.18")


def incl(excl: Decimal) -> Decimal:
    return (excl * VAT_MULT).quantize(Decimal("0.01"))


# ── Data definitions ─────────────────────────────────────────────────────────

CATEGORIES = [
    ("Nargile Takımları", "nargile-takimlari"),
    ("Kömürler", "komurler"),
    ("Lüleler & Aksesuarlar", "luleler-aksesuarlar"),
]

#  (base_code, name, brand, description, category_slug, tags, variants)
#  variant = (sku, label_attr, cost_price, segment_prices, retail_stock, b2b_stock)
#  segment_prices = {RETAIL: excl, CAFE: excl, WHOLESALE: excl, DEALER: excl}

PRODUCTS = [
    (
        "LH013",
        "Premium Çelik Nargile Takımı",
        "DemonHookah",
        "Yüksek kalite paslanmaz çelik gövde, cam kolben, silikon hortum seti.",
        "nargile-takimlari",
        [],
        [
            (
                "LH013-SLV",
                {"color": "Gümüş", "material": "Çelik"},
                Decimal("1200.00"),
                {
                    SegmentType.RETAIL: Decimal("2500.00"),
                    SegmentType.CAFE: Decimal("2100.00"),
                    SegmentType.WHOLESALE: Decimal("1800.00"),
                    SegmentType.DEALER: Decimal("1600.00"),
                },
                50,
                150,
            ),
            (
                "LH013-BLK",
                {"color": "Siyah", "material": "Çelik"},
                Decimal("1200.00"),
                {
                    SegmentType.RETAIL: Decimal("2500.00"),
                    SegmentType.CAFE: Decimal("2100.00"),
                    SegmentType.WHOLESALE: Decimal("1800.00"),
                    SegmentType.DEALER: Decimal("1600.00"),
                },
                50,
                150,
            ),
        ],
    ),
    (
        "KC010",
        "Hindistan Cevizi Kabuğu Kömür 10kg",
        "CocoNara",
        "Doğal hindistan cevizi kabuğundan üretilmiş, kokusuz, uzun yanma süreli nargile kömürü.",
        "komurler",
        ["HEAVY_CARGO"],
        [
            (
                "KC010-10KG",
                {"weight": "10kg", "type": "Küp"},
                Decimal("350.00"),
                {
                    SegmentType.RETAIL: Decimal("750.00"),
                    SegmentType.CAFE: Decimal("600.00"),
                    SegmentType.WHOLESALE: Decimal("500.00"),
                    SegmentType.DEALER: Decimal("450.00"),
                },
                200,
                1000,
            ),
        ],
    ),
    (
        "BW005",
        "Profesyonel Toprak Lüle",
        "HookahJohn",
        "El yapımı sırsız toprak lüle, ısı yönetimi için ideal gözenek yapısı.",
        "luleler-aksesuarlar",
        [],
        [
            (
                "BW005-UNG",
                {"finish": "Sırsız", "material": "Toprak"},
                Decimal("60.00"),
                {
                    SegmentType.RETAIL: Decimal("200.00"),
                    SegmentType.CAFE: Decimal("150.00"),
                    SegmentType.WHOLESALE: Decimal("120.00"),
                    SegmentType.DEALER: Decimal("100.00"),
                },
                100,
                300,
            ),
        ],
    ),
]


async def seed():
    async with async_session() as session:
        # ── Load segment map ─────────────────────────────────────────────
        result = await session.execute(select(CustomerSegment))
        seg_map: dict[SegmentType, int] = {
            seg.code: seg.id for seg in result.scalars().all()
        }
        if len(seg_map) < 4:
            print("ERROR: Customer segments not seeded. Run seed_initial_data.py first.")
            return
        print(f"  Segments loaded: {list(seg_map.keys())}")

        # ── Categories ───────────────────────────────────────────────────
        cat_map: dict[str, int] = {}
        for name, slug in CATEGORIES:
            existing = await session.execute(
                select(Category).where(Category.slug == slug)
            )
            cat = existing.scalar_one_or_none()
            if cat:
                cat_map[slug] = cat.id
                print(f"  ~ Category exists: {name}")
            else:
                cat = Category(name=name, slug=slug)
                session.add(cat)
                await session.flush()
                cat_map[slug] = cat.id
                print(f"  + Category: {name}")

        await session.commit()

        # ── Products + Variants + Prices + Inventory ─────────────────────
        for base_code, name, brand, desc, cat_slug, tags, variants in PRODUCTS:
            existing = await session.execute(
                select(Product).where(Product.base_code == base_code)
            )
            product = existing.scalar_one_or_none()

            if product:
                print(f"\n  ~ Product exists: {name} ({base_code})")
            else:
                product = Product(
                    base_code=base_code,
                    name=name,
                    brand=brand,
                    description=desc,
                    category_id=cat_map[cat_slug],
                    tags=tags,
                )
                session.add(product)
                await session.flush()
                print(f"\n  + Product: {name} ({base_code})")
                if tags:
                    print(f"    Tags: {tags}")

            for sku, attrs, cost, seg_prices, retail_qty, b2b_qty in variants:
                existing_v = await session.execute(
                    select(ProductVariant).where(ProductVariant.sku == sku)
                )
                variant = existing_v.scalar_one_or_none()

                if variant:
                    print(f"    ~ Variant exists: {sku}")
                    continue

                variant = ProductVariant(
                    product_id=product.id,
                    sku=sku,
                    attributes=attrs,
                    cost_price=cost,
                )
                session.add(variant)
                await session.flush()
                print(f"    + Variant: {sku}  attrs={attrs}  cost={cost}")

                # Pricing matrix — all 4 segments
                for seg_type, price_excl in seg_prices.items():
                    session.add(
                        VariantPrice(
                            variant_id=variant.id,
                            segment_id=seg_map[seg_type],
                            currency=CurrencyType.TRY,
                            price_excl_tax=price_excl,
                            price_incl_tax=incl(price_excl),
                            vat_rate=VAT_RATE,
                        )
                    )
                print(
                    f"      Prices: "
                    f"RETAIL={seg_prices[SegmentType.RETAIL]}, "
                    f"CAFE={seg_prices[SegmentType.CAFE]}, "
                    f"WHOLESALE={seg_prices[SegmentType.WHOLESALE]}, "
                    f"DEALER={seg_prices[SegmentType.DEALER]} (excl KDV)"
                )

                # Dual-warehouse inventory
                session.add(Inventory(
                    variant_id=variant.id,
                    warehouse_type=WarehouseType.RETAIL_STOCK,
                    quantity=retail_qty,
                ))
                session.add(Inventory(
                    variant_id=variant.id,
                    warehouse_type=WarehouseType.B2B_STOCK,
                    quantity=b2b_qty,
                ))
                print(f"      Stock: RETAIL={retail_qty}, B2B={b2b_qty}")

            await session.commit()

    print("\n══ Demo catalog seed complete. ══")


if __name__ == "__main__":
    asyncio.run(seed())
