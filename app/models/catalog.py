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
from sqlalchemy import func
from sqlalchemy import event, select
from sqlalchemy import true as sa_true
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, Session, mapped_column, object_session, relationship

from app.core.database import Base
from app.core.slugs import PRODUCT_SLUG_MAX, RESERVED_ROOT_PATHS, product_slug_base


class CurrencyType(str, enum.Enum):
    TRY = "TRY"
    USD = "USD"
    EUR = "EUR"


class WarehouseType(str, enum.Enum):
    """Where an inventory row's stock sits.

    RETAIL_STOCK is the shop's one stock, which every buyer draws from
    (`checkout_engine.CENTRAL_STOCK`). The other three are the per-segment
    quotas (Kafe / Toptan / Bayi) it replaced: kept so their old rows and the
    order lines taken from them still load, and neither read nor written now.
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
    # Off = kept with its products but hidden from the storefront's menus and
    # category list (and its page), without having to move anything out first.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())
    # A cover photo for the category's showcase tile; null = borrow a product photo.
    image_url: Mapped[str | None] = mapped_column(String(500))
    # The category page's introduction under its heading (clamped there, with
    # "Devamını Oku") and its meta description's source; null = the
    # storefront's built-in copy for the slug, if any.
    description: Mapped[str | None] = mapped_column(Text)
    # The passage closing the category's page, in place of the shop's own
    # ("Alt SEO Metni"): its heading, and its text (HTML from the panel's
    # editor, cleaned); both null = the shop's.
    seo_title: Mapped[str | None] = mapped_column(String(160))
    seo_text: Mapped[str | None] = mapped_column(Text)
    # The name and description in the storefront's other languages:
    # {"en": {"name", "description"}, "ar": {...}} (app.schemas.i18n). Missing =
    # the Turkish above.
    translations: Mapped[dict | None] = mapped_column(JSONB)

    parent: Mapped["Category | None"] = relationship(
        remote_side="Category.id", back_populates="children"
    )
    children: Mapped[list["Category"]] = relationship(back_populates="parent")
    products: Mapped[list["Product"]] = relationship(back_populates="category")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (Index("ix_products_slug", "slug", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    base_code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # The page's address: /product/<slug>. Given once, from the name, when the
    # product is first written (see _give_product_a_slug) — or the old store's
    # own address for an imported product — and never changed by a rename, so a
    # link once shared or indexed keeps working.
    slug: Mapped[str] = mapped_column(String(PRODUCT_SLUG_MAX), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(100))
    # "Açıklama ve SEO" on the product sheet. `description` is the storefront's
    # own copy; the two meta fields override the page's <title>/<meta
    # description>, which otherwise fall back to the name and the description.
    description: Mapped[str | None] = mapped_column(Text)
    meta_title: Mapped[str | None] = mapped_column(String(70))
    meta_description: Mapped[str | None] = mapped_column(String(160))
    # The four fields above in the storefront's other languages:
    # {"en": {"name", "description", "meta_title", "meta_description"}, "ar": {...}}
    # (app.schemas.i18n). Missing = the Turkish.
    translations: Mapped[dict | None] = mapped_column(JSONB)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    tags: Mapped[list | None] = mapped_column(JSONB, default=list)
    # The cover photo: always the first of `image_urls`, kept here so cards,
    # thumbnails and metadata keep reading one field.
    image_url: Mapped[str | None] = mapped_column(String(500))
    # Every photo of the product, in gallery order (the cover first).
    image_urls: Mapped[list | None] = mapped_column(JSONB)
    youtube_video_url: Mapped[str | None] = mapped_column(String(500))
    # "Renk" on the product sheet, in the admin's order. The variant axis: one
    # `ProductVariant` per label, each with its own four stock quotas. Kept on the
    # product as well so a reader that only wants the labels needs no join; the
    # product sheet is the single writer and keeps the two in step.
    color_names: Mapped[list | None] = mapped_column(JSONB)
    # Off the storefront entirely while false; the admin lists show it either way.
    is_published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_true()
    )
    # When the product row was written and last changed: the sitemap's
    # <lastmod>. A price or stock change lives on the variant rows and does not
    # move it; a name, text, photo or rule edit on the product sheet does.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # ── Commercial rules, per product ("Genel Kurallar" on the sheet) ─────────
    # Carriage is on the shop for this product whatever the cart total.
    is_free_shipping: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa_false())
    # "Adet şartı": free carriage only from this many units; null = no condition.
    free_shipping_min_quantity: Mapped[int | None] = mapped_column(Integer)
    # Takes part in the havale/EFT discount.
    has_havale_discount: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())
    # "Bayide / Toptanda / Kafede Havale / EFT İndirimi": the havale/EFT discount
    # also applies, line by line, when that B2B list buys it (checkout_engine.
    # HAVALE_SWITCH). Off by default: the discount is a retail perk
    # (app/services/perks.py), and a tier price is costed without it.
    has_dealer_havale_discount: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false()
    )
    has_toptan_havale_discount: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false()
    )
    has_cafe_havale_discount: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false()
    )
    # Counts towards (and is discounted by) the volume discount ("ciro indirimi").
    has_volume_discount: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())
    # Earns Sultan Puan when the order is paid.
    gives_points: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())
    # Points may be spent on it.
    can_use_points: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa_true())
    # "Sepet Kuralı": counts as coal for the cargo rule — a cart where such products
    # are over coal_max_ratio of the subtotal ships free only from coal_min_kg.
    # The product's category plays no part (app.services.checkout_engine).
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

    # The primary category: the first of the product's categories, the one its
    # page's breadcrumb shows. Membership is this *and* `category_links` (see
    # services/categories.products_in) — a product can sit in several.
    category: Mapped[Category | None] = relationship(back_populates="products")
    category_links: Mapped[list["ProductCategory"]] = relationship(
        order_by="ProductCategory.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    # The colours, in the order the admin arranged them (see ProductVariant).
    variants: Mapped[list["ProductVariant"]] = relationship(
        back_populates="product", order_by="ProductVariant.position, ProductVariant.sku"
    )
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


class ProductCategory(Base):
    """One category a product is listed under, beyond (or including) its primary.

    The sheet writes every category the admin ticked here, primary first; rows
    written before this table existed only have `products.category_id`, which is
    why membership reads both.
    """

    __tablename__ = "product_categories"
    __table_args__ = (Index("ix_product_categories_category", "category_id"),)

    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), primary_key=True
    )
    # No cascade: a category with products cannot be deleted (admin_taxonomy).
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), primary_key=True)
    # The order the admin ticked them in (0 = primary).
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")



class CategoryProductOrder(Base):
    """The admin's own order of a category page's products ("Önerilen"): a
    position per product, per category — a group page has its own, apart from
    its subcategories'. Ranked products come first, in this order; the rest
    follow in the catalog's usual order (by name). Rows go with their product
    or category.
    """

    __tablename__ = "category_product_orders"
    __table_args__ = (Index("ix_category_product_orders_position", "category_id", "position"),)

    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True)
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class ProductVariant(Base):
    """One buyable colour of a product — the thing stock, prices and orders hang off.

    A product with "Renk" labels has one variant per label; one with none has a
    single variant whose `color_name` is NULL (the "Standart" variant). The
    customer picks a colour on the product page and buys *that* variant, so each
    colour carries its own four stock quotas in `inventory`.

    Prices are per variant in the schema but product-wide in practice: the sheet
    has one price matrix and writes the same rows to every colour. Keeping the
    column here leaves room to price a colour differently later without another
    migration.

    A colour the admin removes is deleted when it was never ordered, and
    otherwise kept with `is_active = False`: its order lines still have to name
    the thing that was sold. Re-adding the same label revives that row, with its
    history, instead of making a second one — which the unique constraint on
    (product, colour) forbids anyway.
    """

    __tablename__ = "product_variants"
    __table_args__ = (
        Index("ix_product_variants_sku", "sku", unique=True),
        # One row per colour per product. NULLs are distinct in Postgres, so this
        # does not by itself stop two "Standart" variants; the product sheet does.
        UniqueConstraint("product_id", "color_name", name="uq_variant_product_color"),
        Index("ix_product_variants_product", "product_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id"), nullable=False
    )
    sku: Mapped[str] = mapped_column(String(50), nullable=False)
    barcode: Mapped[str | None] = mapped_column(String(50))
    # The "Renk" label this variant is, exactly as the admin typed it; NULL on a
    # product that has no colour axis.
    color_name: Mapped[str | None] = mapped_column(String(40))
    # Order among the product's colours (0-based), as the admin arranged the pills.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # False once the colour is retired: off the storefront and off the sheet, but
    # still readable by the orders that contain it.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_true()
    )
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



class SlugRedirect(Base):
    """An address a product or category had before an admin changed its slug.

    The old address keeps answering — the storefront sends its visitor (and
    search engines) on to the current one for good — because it may already be
    indexed, bookmarked or printed. It points at the row, not at the new slug,
    so a second rename needs no chain of redirects. A slug taken back into live
    use stops being a redirect (see app.services.slug_redirects).
    """

    __tablename__ = "slug_redirects"
    __table_args__ = (
        UniqueConstraint("kind", "old_slug", name="uq_slug_redirect"),
        CheckConstraint("(product_id IS NULL) <> (category_id IS NULL)", name="ck_slug_redirect_one_target"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # "product" (/product/<slug>) or "category" (/catalog/<slug>).
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    old_slug: Mapped[str] = mapped_column(String(PRODUCT_SLUG_MAX), nullable=False)
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE")
    )
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

# ── Product slugs ─────────────────────────────────────────────────────────────

_PENDING_SLUGS = "pending_product_slugs"


@event.listens_for(Product, "before_insert")
def _give_product_a_slug(mapper, connection, target: Product) -> None:
    """A new product without a slug gets one from its code and name (see
    product_slug_base) — unique by its code; "-2", "-3"… only as a failsafe.

    Here rather than in each writer (the product sheet, the plain create
    endpoint, the import scripts, the seeds), so none of them can forget it.
    Slugs handed out earlier in the same flush are not in the table yet, so the
    session remembers them until the flush is over.
    """
    if target.slug:
        return
    session = object_session(target)
    pending: set[str] = session.info.setdefault(_PENDING_SLUGS, set()) if session is not None else set()
    base = product_slug_base(target.name or "", target.base_code or "")
    taken = set(
        connection.execute(
            select(Product.slug).where((Product.slug == base) | Product.slug.like(f"{base}-%"))
        ).scalars()
    ) | pending
    slug, n = base, 2
    # One of the storefront's own root addresses counts as taken (/blog → "blog-2").
    while slug in taken or slug in RESERVED_ROOT_PATHS:
        slug, n = f"{base}-{n}", n + 1
    pending.add(slug)
    target.slug = slug


@event.listens_for(Session, "after_flush")
def _forget_pending_slugs(session, flush_context) -> None:
    session.info.pop(_PENDING_SLUGS, None)
