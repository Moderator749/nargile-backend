"""The pricing worksheet ("Fiyat Hesaplama Cetveli") as the panel sends it."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.catalog import CurrencyType
from app.models.user import SegmentType

Money = Decimal
VatMode = Literal["EXCL", "INCL"]


class PricingColumnIn(BaseModel):
    """A price class between the cost and retail: "Bayi Fiyatı", "Toptan Fiyatı"…"""

    label: str = Field(min_length=1, max_length=40)
    # The customer group whose live price this column sets; None for a step that
    # is only part of the arithmetic.
    segment: SegmentType | None = None
    # On top of the previous column's price.
    percent: Decimal = Field(ge=-99.99, le=10000, decimal_places=4)
    # "Uygulanacak fiyat": the admin's rounded or chosen figure, sent to the site
    # in place of the calculated one. It does not feed the next column: the
    # calculated row is one unbroken chain. Empty: the calculated value.
    desired: Money | None = Field(default=None, gt=0, le=Decimal("100000000"), decimal_places=2)

    @field_validator("label")
    @classmethod
    def _strip(cls, v: str) -> str:
        return " ".join(v.split())


class PricingRetailIn(BaseModel):
    """The last column, always retail ("KDV dahil perakende fiyat"): after VAT,
    calculated and typed alike."""

    percent: Decimal = Field(ge=-99.99, le=10000, decimal_places=4)
    # The VAT rate written with the prices, to complete their other side.
    vat_rate: Decimal = Field(ge=0, le=99.99, decimal_places=2)
    desired: Money | None = Field(default=None, gt=0, le=Decimal("100000000"), decimal_places=2)


class PricingExtraIn(BaseModel):
    """A price after retail ("Özel Fiyat 1"…), worked out as retail is: the
    previous step plus its percent, plus VAT on a before-VAT sheet. For the
    admin's own reference: no customer group's price, never sent to the site."""

    label: str = Field(min_length=1, max_length=40)
    percent: Decimal = Field(ge=-99.99, le=10000, decimal_places=4)
    desired: Money | None = Field(default=None, gt=0, le=Decimal("100000000"), decimal_places=2)

    @field_validator("label")
    @classmethod
    def _strip(cls, v: str) -> str:
        return " ".join(v.split())


class PricingSheetIn(BaseModel):
    product_code: str = Field(default="", max_length=60)
    product_name: str = Field(default="", max_length=255)
    # Whether the cost — and so every column — is before or after VAT.
    vat_mode: VatMode = "EXCL"
    currency: CurrencyType = CurrencyType.TRY
    cost: Money = Field(gt=0, le=Decimal("100000000"), decimal_places=4)
    columns: list[PricingColumnIn] = Field(default_factory=list, max_length=12)
    retail: PricingRetailIn
    extras: list[PricingExtraIn] = Field(default_factory=list, max_length=6)
    # TRY per unit, typed in (prefilled from TCMB) when the sheet is in USD/EUR.
    usd_rate: Decimal | None = Field(default=None, gt=0, le=1000, decimal_places=4)
    eur_rate: Decimal | None = Field(default=None, gt=0, le=1000, decimal_places=4)
    # "Son karar": the TRY retail price decided on, for a USD/EUR sheet.
    final_try: Money | None = Field(default=None, gt=0, le=Decimal("100000000"), decimal_places=2)

    @field_validator("product_code", "product_name")
    @classmethod
    def _trim(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _segments_once(self) -> "PricingSheetIn":
        used = [c.segment for c in self.columns if c.segment is not None]
        if SegmentType.RETAIL in used:
            raise ValueError("Perakende fiyatı son sütundur; ara sütunlar perakendeye yazamaz")
        if len(used) != len(set(used)):
            raise ValueError("Bir müşteri grubu yalnızca bir sütuna bağlanabilir")
        return self


class SaveTarget(BaseModel):
    """An existing file, or the name of a new one ("Dosyaya kaydet")."""

    file_id: int | None = None
    file_name: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _one(self) -> "SaveTarget":
        name = " ".join((self.file_name or "").split())
        self.file_name = name or None
        if (self.file_id is None) == (self.file_name is None):
            raise ValueError("Bir dosya seçin ya da yeni dosya adı girin")
        return self


class SaveDraftRequest(BaseModel):
    sheet: PricingSheetIn


class SaveToFileRequest(SaveTarget):
    sheet: PricingSheetIn


class PricingFileRead(BaseModel):
    id: int
    name: str
    created_at: datetime
    # Last time a row was saved into it.
    updated_at: datetime
    record_count: int = 0


class PricingRecordRead(BaseModel):
    id: uuid.UUID
    file_id: int | None
    file_name: str | None
    product_code: str
    product_name: str
    # The product's photo, looked up by its code when the row is read, so it is
    # the current one; None when the code names no product, or one without a photo.
    image_url: str | None = None
    sheet: dict
    applied_at: datetime | None
    applied_summary: dict | None
    created_at: datetime


class AppliedPrice(BaseModel):
    label: str
    segment: SegmentType
    currency: CurrencyType
    price_excl_tax: Decimal
    price_incl_tax: Decimal


class ApplyResult(BaseModel):
    record: PricingRecordRead
    product_id: uuid.UUID
    product_name: str
    variants_updated: int
    prices: list[AppliedPrice]
    # Columns left out: no customer group chosen for them.
    skipped: list[str]


# ── Toplu fiyat (Excel) ───────────────────────────────────────────────────────


class ImportedRow(BaseModel):
    """One block of the workbook: where it is, and its four prices (TL, VAT included)."""

    sheet: str
    row: int
    side: Literal["sol", "sağ"]
    section: str | None
    name: str
    prices: dict[SegmentType, Decimal | None]


class CurrentPrice(BaseModel):
    currency: CurrencyType
    price_incl_tax: Decimal


class ImportCandidate(BaseModel):
    """A product a row may mean, for the admin to confirm."""

    product_id: uuid.UUID
    code: str
    name: str
    score: float
    # "abbreviation": the name with a word spelled the other way (GOLD/G);
    # "similar": a code like it, of the same model; "same_code": one of several
    # products that share the name exactly.
    basis: Literal["abbreviation", "similar", "same_code"]
    retail: CurrentPrice | None
    image_url: str | None


class ImportMatched(ImportedRow):
    product_id: uuid.UUID
    product_code: str
    product_name: str
    # "alias": tied to the product by the admin on an earlier upload.
    matched_by: Literal["alias", "code", "sku", "name"]
    alias_id: int | None
    variants: int
    old_prices: dict[SegmentType, CurrentPrice]
    image_url: str | None


class ImportSuggested(ImportedRow):
    candidates: list[ImportCandidate]
    # Safe to accept in bulk ("Güvenli önerileri eşleştir"): exactly one candidate
    # is the name spelled the other way, or close and already at this retail price.
    recommended_id: uuid.UUID | None


class ImportUnmatched(ImportedRow):
    reason: Literal["unnamed", "incomplete", "not_found", "conflict", "no_variants"]


class ImportSkipped(BaseModel):
    sheet: str
    row: int
    side: Literal["sol", "sağ"]
    name: str
    reason: Literal["foreign_currency", "vat_excluded", "no_dealer_price"]


class PriceImportResult(BaseModel):
    dry_run: bool
    matched: list[ImportMatched]
    # Not priced until the admin confirms one of the candidates.
    suggested: list[ImportSuggested]
    unmatched: list[ImportUnmatched]
    # Blocks in another currency or before VAT: not this importer's to read.
    skipped: list[ImportSkipped]
    updated_products: int
    updated_variants: int


class ImportAliasCreate(BaseModel):
    excel_name: str = Field(min_length=1, max_length=120)
    product_id: uuid.UUID


class ImportAliasBatch(BaseModel):
    aliases: list[ImportAliasCreate] = Field(min_length=1, max_length=200)


class ImportAliasRead(BaseModel):
    id: int
    excel_name: str
    product_id: uuid.UUID
    product_code: str
    product_name: str
    image_url: str | None
    created_at: datetime
