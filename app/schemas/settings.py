"""The shop settings the panel edits, and what the storefront is told."""

from decimal import Decimal

from pydantic import BaseModel, Field

from app.services.shop_settings import ISTANBUL_FREE_MIN_DEFAULT, ISTANBUL_FREE_MIN_MAX


class ShippingSettingsRead(BaseModel):
    """Read without a login: the cart has to say what the limit is before the
    customer picks the option it belongs to."""

    # "İstanbul İçi B2B Ücretsiz Teslimat Alt Limiti (TL)".
    istanbul_free_min_subtotal: Decimal = ISTANBUL_FREE_MIN_DEFAULT


class ShippingSettingsUpdate(BaseModel):
    # Zero is allowed, and means every İstanbul order goes on the van for free.
    istanbul_free_min_subtotal: Decimal = Field(ge=0, le=ISTANBUL_FREE_MIN_MAX)
