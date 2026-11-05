import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.database import async_session
from app.core.media_files import MediaFiles
from app.core.media_rewrite import LocalMediaRewriteMiddleware
from app.core.trailing_slash import StripTrailingSlashMiddleware
from app.services import auto_translate
from app.services.order_tasks import cancel_expired_pending_orders
from app.services.shipping.sync import sync_shipments

logger = logging.getLogger(__name__)


def _app_logging() -> None:
    """The app's own lines (app.*) in the service log at INFO, beside uvicorn's.

    Uvicorn configures only its loggers; without this, app.* has no handler and
    only warnings and errors reach journald (through logging's last resort) —
    a sweep that ran, a translation written, the scheduler starting: none of
    it showed. They still propagate, so pytest's caplog sees them too.
    """
    app_logger = logging.getLogger("app")
    if not any(getattr(h, "_app_log", False) for h in app_logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        handler._app_log = True  # type: ignore[attr-defined]
        app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)


_app_logging()


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


async def _run_translation_sweep() -> None:
    """Scheduled job: English and Arabic drafts for what was saved without them."""
    try:
        count = await auto_translate.sweep()
        if count:
            logger.info("Translation sweep: %d row(s) drafted.", count)
    except Exception:
        logger.exception("Translation sweep failed")


#: Every job: a run up to 5 minutes late still runs (APScheduler's default is
#: 1 second, and on a one-core server a busy moment would skip it silently),
#: and runs missed meanwhile are made up once, not one by one.
JOB_DEFAULTS = {"misfire_grace_time": 300, "coalesce": True}
#: The first translation sweep a minute after startup, not a full interval later.
FIRST_SWEEP_AFTER = timedelta(minutes=1)


def build_scheduler() -> AsyncIOScheduler:
    """The background jobs, not yet started; says once whether auto-translation is on."""
    scheduler = AsyncIOScheduler(job_defaults=JOB_DEFAULTS)
    scheduler.add_job(_run_sla_check, "interval", hours=1, id="sla_check")
    scheduler.add_job(_run_shipment_sync, "interval", hours=1, id="shipment_sync")
    if auto_translate.enabled():
        scheduler.add_job(
            _run_translation_sweep,
            "interval",
            minutes=15,
            id="translation_sweep",
            next_run_time=datetime.now(timezone.utc) + FIRST_SWEEP_AFTER,
        )
        logger.info(
            "Auto-translation on (model %s): first sweep in a minute, then every 15 minutes.",
            settings.AUTO_TRANSLATE_MODEL,
        )
    else:
        logger.warning("Auto-translation off: ANTHROPIC_API_KEY is not set.")
    return scheduler


@asynccontextmanager
async def lifespan(application: FastAPI):
    scheduler = build_scheduler()
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
    # The interactive docs and the schema are for development only: in
    # production (DEBUG off) the API does not describe itself to strangers.
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
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

# Uploaded media. Only validated raster images are ever written here; an
# address to a JPEG/PNG since converted to WebP is sent on to it (MediaFiles).
settings.MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/media", MediaFiles(directory=settings.MEDIA_ROOT), name="media")


@app.get("/health")
async def health():
    return {"status": "ok"}
