import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import false as sa_false
from sqlalchemy import true as sa_true
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class CurrencyType(str, enum.Enum):
    TRY = "TRY"
    USD = "USD"
    EUR = "EUR"


class WarehouseType(str, enum.Enum):
    """One stock quota per customer segment ("Perakende / Kafe / Toptan / Bayi").

    B2B_STOCK is the Toptan (wholesale) quota; it keeps its original name because
    it predates the split, when wholesale and dealers shared one pool.
    """

    RETAIL_STOCK = "RETAIL_STOCK"
    CAFE_STOCK = "CAFE_STOCK"
    B2B_STOCK = "B2B_STOCK"
    DEALER_STOCK = "DEALER_STOCK"


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (Index("ix_categories_slug", "slug", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    slug: Mapped[str] = mapped_column(String(180), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    # Menu order among siblings (ascending; ties by name).
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    parent: Mapped["Category | None"] = relationship(
        remote_side="Category.id", back_populates="children"
    )
    children: Mapped[list["Category"]] = relationship(back_populates="parent")
    products: Mapped[list["Product"]] = relationship(back_populates="category")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    base_code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    tags: Mapped[list | None] = mapped_column(JSONB, default=list)
    # The cover photo: always the first of `image_urls`, kept here so cards,
    # thumbnails and metadata keep reading one field.
    image_url: Mapped[str | None] = mapped_column(String(500))
    # Every photo of the product, in gallery order (the cover first).
    image_urls: Mapped[list | None] = mapped_column(JSONB)
    youtube_video_url: Mapped[str | None] = mapped_column(String(500))
    # "Renk" on the product sheet: free text labels, not a variant axis.
    color_names: Mapped[list | None] = mapped_column(JSONB)
    # Off the storefront entirely while false; the admin lists show it either way.
    is_published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_true()
    )

    # ── Commercial rules, per product ("Genel Kurallar" on the sheet) ─────────
    # Carriage is on the shop for this product whatever the cart total.
    is_free_shipping: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa_false())
    # "Adet şartı": free carriage only from this many units; null = no condition.
    free_shipping_min_quantity: Mapped[int | None] = mapped_column(Integer)
    # Takes part in the havale/EFT discount.
    has_havale_discount: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())
    # Counts towards (and is discounted by) the volume discount ("ciro indirimi").
    has_volume_discount: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())
    # Earns ParaPuan/Kumbarapuan when the order is paid.
    gives_points: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())
    # Points may be spent on it.
    can_use_points: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())
    # "Ağır Kargo rakamı 3.000 TL üzeri alışverişlerde sepetin %50'sini geçemez."
    is_cart_rule_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa_false())

    # ── "Fırsat" (opportunity) block of the same sheet ────────────────────────
    # The live sale price itself lives in flash_sales, which the product form
    # writes from these columns; see app.services.product_blueprint.
    is_opportunity: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa_false())
    opportunity_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    opportunity_stock: Mapped[int | None] = mapped_column(Integer)
    opportunity_end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # "Fırsat rengi": the badge colour on the storefront card, "#rrggbb".
    opportunity_color: Mapped[str | None] = mapped_column(String(20))
    opp_is_free_shipping: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa_false())
    opp_free_shipping_min_quantity: Mapped[int | None] = mapped_column(Integer)
    opp_has_havale_discount: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())
    opp_has_volume_discount: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())
    opp_gives_points: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())
    opp_can_use_points: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())

    category: Mapped[Category | None] = relationship(back_populates="products")
    variants: Mapped[list["ProductVariant"]] = relationship(back_populates="product")
    cross_sells: Mapped[list["ProductCrossSell"]] = relationship(
        foreign_keys="ProductCrossSell.product_id",
        order_by="ProductCrossSell.position",
        cascade="all, delete-orphan",
    )


class ProductCrossSell(Base):
    """"Birlikte satılanlar": products the admin pins to another product's page.

    A join table rather than a list of ids on the product, so that deleting a
    product takes its references with it — a dangling id would show the storefront
    an empty cross-sell slot it could not explain.
    """

    __tablename__ = "product_cross_sells"
    __table_args__ = (
        UniqueConstraint("product_id", "related_product_id", name="uq_cross_sell_pair"),
        CheckConstraint("product_id <> related_product_id", name="ck_cross_sell_not_self"),
        Index("ix_product_cross_sells_product", "product_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    related_product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    # The order the admin arranged them in (0-based).
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    related: Mapped["Product"] = relationship(foreign_keys=[related_product_id])


class ProductVariant(Base):
    __tablename__ = "product_variants"
    __table_args__ = (Index("ix_product_variants_sku", "sku", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id"), nullable=False
    )
    sku: Mapped[str] = mapped_column(String(50), nullable=False)
    barcode: Mapped[str | None] = mapped_column(String(50))
    attributes: Mapped[dict | None] = mapped_column(JSONB)
    cost_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # Weight of one unit in kg (coal's 20 kg free-shipping minimum); None = unknown.
    weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))

    product: Mapped[Product] = relationship(back_populates="variants")
    prices: Mapped[list["VariantPrice"]] = relationship(back_populates="variant")
    inventory: Mapped[list["Inventory"]] = relationship(back_populates="variant")


class VariantPrice(Base):
    __tablename__ = "variant_prices"
    __table_args__ = (
        Index(
            "ix_variant_prices_unique",
            "variant_id",
            "segment_id",
            "currency",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    variant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_variants.id"), nullable=False
    )
    segment_id: Mapped[int] = mapped_column(
        ForeignKey("customer_segments.id"), nullable=False
    )
    currency: Mapped[str] = mapped_column(
        Enum(CurrencyType), default=CurrencyType.TRY, nullable=False
    )
    price_excl_tax: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    price_incl_tax: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)

    variant: Mapped[ProductVariant] = relationship(back_populates="prices")


class Inventory(Base):
    __tablename__ = "inventory"
    __table_args__ = (
        Index("ix_inventory_variant_warehouse", "variant_id", "warehouse_type", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    variant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_variants.id"), nullable=False
    )
    warehouse_type: Mapped[str] = mapped_column(
        Enum(WarehouseType), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    variant: Mapped[ProductVariant] = relationship(back_populates="inventory")
