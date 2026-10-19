import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.database import async_session
from app.core.media_rewrite import LocalMediaRewriteMiddleware
from app.core.trailing_slash import StripTrailingSlashMiddleware
from app.services.order_tasks import cancel_expired_pending_orders
from app.services.shipping.sync import sync_shipments

logger = logging.getLogger(__name__)


async def _run_sla_check() -> None:
    """Scheduled job: cancel expired bank-transfer orders."""
    async with async_session() as db:
        try:
            count = await cancel_expired_pending_orders(db)
            if count:
                logger.info("SLA job: cancelled %d expired order(s).", count)
        except Exception:
            logger.exception("SLA job failed")


async def _run_shipment_sync() -> None:
    """Scheduled job: Yurtiçi tracking numbers and statuses for open shipments."""
    async with async_session() as db:
        try:
            count = await sync_shipments(db)
            if count:
                logger.info("Shipment sync: %d shipment(s) updated.", count)
        except Exception:
            logger.exception("Shipment sync failed")


@asynccontextmanager
async def lifespan(application: FastAPI):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_run_sla_check, "interval", hours=1, id="sla_check")
    scheduler.add_job(_run_shipment_sync, "interval", hours=1, id="shipment_sync")
    scheduler.start()
    logger.info("SLA scheduler started (every 1 hour).")
    yield
    scheduler.shutdown()
    logger.info("SLA scheduler stopped.")


app = FastAPI(
    title="Nargile Malzemesi API",
    version="0.1.0",
    debug=settings.DEBUG,
    lifespan=lifespan,
    # Never answer with a slash redirect: its Location carries the Host the API
    # was reached on, which behind the storefront's proxy is not the page's
    # (see app/core/trailing_slash.py). Both spellings are served instead.
    redirect_slashes=False,
)
app.add_middleware(StripTrailingSlashMiddleware)

if settings.REWRITE_LOCAL_MEDIA_URLS:
    app.add_middleware(
        LocalMediaRewriteMiddleware,
        local_origins=settings.LOCAL_MEDIA_ORIGINS,
        public_url=settings.PUBLIC_MEDIA_URL,
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_origin_regex=settings.CORS_ORIGIN_REGEX,
    allow_methods=["*"],
    allow_headers=["*"],
    # Paginated admin lists report their total here.
    expose_headers=["X-Total-Count"],
)

app.include_router(api_router)

# Uploaded media (brand logos). Only validated raster images are ever written here.
settings.MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=settings.MEDIA_ROOT), name="media")


@app.get("/health")
async def health():
    return {"status": "ok"}
