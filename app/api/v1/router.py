from fastapi import APIRouter

from app.api.v1.announcements import router as announcements_router
from app.api.v1.auth import router as auth_router
from app.api.v1.categories import router as categories_router
from app.api.v1.products import router as products_router
from app.api.v1.orders import router as orders_router
from app.api.v1.account import router as account_router
from app.api.v1.admin_catalog import router as admin_catalog_router
from app.api.v1.admin_customers import router as admin_customers_router
from app.api.v1.admin_dashboard import router as admin_dashboard_router
from app.api.v1.admin_orders import router as admin_orders_router
from app.api.v1.admin_promotions import router as admin_promotions_router
from app.api.v1.admin_taxonomy import router as admin_taxonomy_router
from app.api.v1.admin_uploads import router as admin_uploads_router
from app.api.v1.brands import router as brands_router
from app.api.v1.home_content import admin as admin_home_content_router
from app.api.v1.home_content import router as home_content_router
from app.api.v1.home_settings import router as home_settings_router
from app.api.v1.legal import router as legal_router
from app.api.v1.settings import router as settings_router
from app.api.v1.exchange_rates import router as exchange_rates_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router)
api_router.include_router(categories_router)
api_router.include_router(products_router)
api_router.include_router(orders_router)
api_router.include_router(admin_catalog_router)
api_router.include_router(admin_orders_router)
api_router.include_router(announcements_router)
api_router.include_router(admin_promotions_router)
api_router.include_router(exchange_rates_router)
api_router.include_router(brands_router)
api_router.include_router(admin_taxonomy_router)
api_router.include_router(home_content_router)
api_router.include_router(admin_home_content_router)
api_router.include_router(home_settings_router)
api_router.include_router(account_router)
api_router.include_router(admin_customers_router)
api_router.include_router(admin_dashboard_router)
api_router.include_router(admin_uploads_router)
api_router.include_router(legal_router)
api_router.include_router(settings_router)
