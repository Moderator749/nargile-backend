"""The invoice details a customer keeps on file.

Checkout asks for these — "Faturayı firmaya/şirkete istiyorum" — and can save
them back to the account, so the next order starts filled in. The order keeps
its own frozen copy either way: this is the account's standing answer, not the
history of what was issued.
"""

from pydantic import BaseModel, Field, field_validator

from app.core.tr_identity import normalize_tax_id
from app.schemas.address import InvoiceAddress


class CorporateInvoiceDetails(BaseModel):
    """The firm an individual asks to be invoiced instead.

    In the order the cart asks for it: the ünvan on the tax certificate, the
    name the firm trades under, where it is registered, and its tax identity.
    """

    company_title: str = Field(min_length=2, max_length=200)  # Firma Ünvanı
    # "Firma Adı": optional, since plenty of firms are known by their ünvan.
    trade_name: str | None = Field(default=None, max_length=200)
    address: InvoiceAddress  # Şirket resmi adresi, il ve ilçe
    tax_office: str = Field(min_length=2, max_length=100)  # Vergi dairesi
    # VKN (10 digits) or a sole proprietor's TCKN (11), checksum and all.
    tax_number: str = Field(max_length=20)  # Vergi No / TC

    @field_validator("company_title", "tax_office", mode="before")
    @classmethod
    def _collapse(cls, value: str) -> str:
        # Before the length checks, so whitespace alone cannot pass them.
        return " ".join(value.split()) if isinstance(value, str) else value

    @field_validator("trade_name", mode="before")
    @classmethod
    def _optional(cls, value: str | None) -> str | None:
        if not isinstance(value, str):
            return value
        return " ".join(value.split()) or None

    @field_validator("tax_number")
    @classmethod
    def _tax_number(cls, value: str) -> str:
        return normalize_tax_id(value)
