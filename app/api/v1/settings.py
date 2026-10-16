"""Shop settings: read by the storefront, written from the admin panel.

The İstanbul delivery limit ("Teslimat Ayarları") and the store parameters
("Parametreler": free shipping, the coal rule, the havale and ciro ladders).
Both reads are public because the storefront states these figures — beside the
shipping option, in the free-shipping bar, under the payment method — to
customers who are not logged in as well.
"""

from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.settings import ShippingSettingsRead, ShippingSettingsUpdate, StoreParametersSchema
from app.services.shop_settings import (
    ISTANBUL_FREE_MIN_KEY,
    istanbul_free_minimum,
    save_setting,
)
from app.services.store_parameters import load_parameters, save_parameters

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


@router.get("/settings/parameters", response_model=StoreParametersSchema)
async def get_store_parameters(db: AsyncSession = Depends(get_db)):
    return StoreParametersSchema.of(await load_parameters(db))


@router.get("/admin/settings/parameters", response_model=StoreParametersSchema)
async def admin_get_store_parameters(
    _admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    return StoreParametersSchema.of(await load_parameters(db))


@router.put("/admin/settings/parameters", response_model=StoreParametersSchema)
async def admin_update_store_parameters(
    payload: StoreParametersSchema,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace every parameter at once; the next cart calculation uses them."""
    await save_parameters(db, payload.to_parameters(), admin.id)
    await db.commit()
    return StoreParametersSchema.of(await load_parameters(db))
