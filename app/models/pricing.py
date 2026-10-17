""""Fiyat Hesaplama Cetveli": the admin's pricing worksheets.

A worksheet row (a "kayıt") is one product priced from its cost through each
price class to retail. Rows without a file are the drafts ("Taslaklar"); a
file ("Dosya") is a named folder of rows. The row keeps its whole worksheet as
JSON — the columns are the admin's own, added and renamed per row — and, once
it has been applied to the live product, when and what it changed.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PricingFile(Base):
    __tablename__ = "pricing_files"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    # When a row was last saved into it (its creation, until then).
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class PricingRecord(Base):
    __tablename__ = "pricing_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Null: a draft ("Taslaklar"). Deleting a file deletes its rows.
    file_id: Mapped[int | None] = mapped_column(
        ForeignKey("pricing_files.id", ondelete="CASCADE"), index=True
    )
    product_code: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    product_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # The worksheet as entered, with every column's calculated value.
    sheet: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Set when "Dosya ve Sitede Ürüne Kaydet" wrote it to the live product.
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_summary: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class PriceImportAlias(Base):
    """A name from the pricing workbook the admin has tied to a product.

    The workbook names products its own way ("NT10 GOLD" for the product coded
    "NT10 G"); a suggestion confirmed once in "Toplu Fiyat" is kept here and
    matched first on every later upload. `key` is the name as compared
    (`price_import.normalize`), `excel_name` as it was written.
    """

    __tablename__ = "price_import_aliases"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    excel_name: Mapped[str] = mapped_column(String(120), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
