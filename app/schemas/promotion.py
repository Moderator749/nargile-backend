import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, Field, model_validator


class FlashSaleWrite(BaseModel):
    """Create or replace a flash sale. The variant is picked by SKU."""

    sku: str = Field(min_length=1, max_length=50)
    sale_price_incl_tax: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    # Timezone required: a naive time would be read in the server's zone, not the admin's.
    starts_at: AwareDatetime
    # None: no deadline — the offer runs until it is switched off (or its deal
    # stock runs out), as the database allows and the deals backfill wrote.
    ends_at: AwareDatetime | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def _window(self) -> "FlashSaleWrite":
        self.sku = self.sku.strip()
        if self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class FlashSaleRead(BaseModel):
    id: int
    variant_id: uuid.UUID
    sku: str
    product_name: str
    # Which colour of it is on offer; null on a colourless product. A coloured
    # product has one sale row per colour, so this is what tells the rows apart.
    color_name: str | None = None
    sale_price_incl_tax: Decimal
    # Public RETAIL price, for the admin's "was/now" check; None if unpriced.
    regular_price_incl_tax: Decimal | None
    starts_at: datetime
    ends_at: datetime | None = None  # no deadline
    is_active: bool
    is_live: bool
