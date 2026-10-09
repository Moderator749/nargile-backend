"""Shop settings: read by the storefront, written from the admin panel.

Only the shipping limit lives here so far. The read is public because the cart
shows the figure ("₺20.000 ve üzeri alışverişlerde ücretsiz") beside the option
it governs, and a customer who is not logged in still sees the cart.
"""

from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.settings import ShippingSettingsRead, ShippingSettingsUpdate
from app.services.shop_settings import (
    ISTANBUL_FREE_MIN_KEY,
    istanbul_free_minimum,
    save_setting,
)

router = APIRouter(tags=["settings"])


async def _shipping_settings(db: AsyncSession) -> ShippingSettingsRead:
    return ShippingSettingsRead(istanbul_free_min_subtotal=await istanbul_free_minimum(db))


@router.get("/settings/shipping", response_model=ShippingSettingsRead)
async def get_shipping_settings(db: AsyncSession = Depends(get_db)):
    return await _shipping_settings(db)


@router.get("/admin/settings/shipping", response_model=ShippingSettingsRead)
async def admin_get_shipping_settings(
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    return await _shipping_settings(db)


@router.put("/admin/settings/shipping", response_model=ShippingSettingsRead)
async def admin_update_shipping_settings(
    payload: ShippingSettingsUpdate,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    amount: Decimal = payload.istanbul_free_min_subtotal.quantize(Decimal("0.01"))
    await save_setting(db, ISTANBUL_FREE_MIN_KEY, f"{amount}", admin.id)
    await db.commit()
    return await _shipping_settings(db)
