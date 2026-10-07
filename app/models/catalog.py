import enum
import uuid
from decimal import Decimal

from sqlalchemy import Enum, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class CurrencyType(str, enum.Enum):
    TRY = "TRY"
    USD = "USD"
    EUR = "EUR"


class WarehouseType(str, enum.Enum):
    RETAIL_STOCK = "RETAIL_STOCK"
    B2B_STOCK = "B2B_STOCK"


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
    image_url: Mapped[str | None] = mapped_column(String(500))

    category: Mapped[Category | None] = relationship(back_populates="products")
    variants: Mapped[list["ProductVariant"]] = relationship(back_populates="product")


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
