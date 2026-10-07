import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.catalog import ProductVariant


class FlashSale(Base):
    """Time-boxed sale price for one variant ("Fırsat Ürünleri").

    Live while is_active and starts_at <= now < ends_at. Checkout charges
    min(segment price, sale price), so a sale never raises anyone's price, and
    the price reverts on its own once ends_at passes.
    """

    __tablename__ = "flash_sales"
    __table_args__ = (
        CheckConstraint("sale_price_incl_tax > 0", name="ck_flash_sales_price_positive"),
        CheckConstraint("ends_at > starts_at", name="ck_flash_sales_window"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # One sale per variant: re-running a deal edits the row instead of stacking prices.
    variant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_variants.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    sale_price_incl_tax: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    variant: Mapped[ProductVariant] = relationship()

    def is_live(self, now: datetime) -> bool:
        return self.is_active and self.starts_at <= now < self.ends_at
