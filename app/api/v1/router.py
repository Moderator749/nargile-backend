from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.products import router as products_router
from app.api.v1.orders import router as orders_router
from app.api.v1.admin_catalog import router as admin_catalog_router
from app.api.v1.admin_orders import router as admin_orders_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router)
api_router.include_router(products_router)
api_router.include_router(orders_router)
api_router.include_router(admin_catalog_router)
api_router.include_router(admin_orders_router)
