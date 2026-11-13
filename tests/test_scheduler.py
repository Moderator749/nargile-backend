"""The background jobs: a late run still runs, the translation sweep comes a
minute after startup, and the log says whether auto-translation is on."""

import logging
from datetime import datetime, timedelta, timezone

from app import main
from app.core.config import settings


def _jobs(scheduler) -> dict:
    """The jobs as the running app has them: started paused (the defaults are
    applied on start), so none runs here."""
    scheduler.start(paused=True)
    try:
        return {job.id: job for job in scheduler.get_jobs()}
    finally:
        scheduler.shutdown(wait=False)


async def test_every_job_tolerates_a_late_start_and_coalesces():
    jobs = _jobs(main.build_scheduler())
    assert {"sla_check", "shipment_sync"} <= set(jobs)
    for job in jobs.values():
        assert job.misfire_grace_time == 300 and job.coalesce is True


async def test_with_a_key_the_sweep_runs_a_minute_after_startup(monkeypatch, caplog):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key")
    before = datetime.now(timezone.utc)
    with caplog.at_level(logging.INFO, logger="app"):
        jobs = _jobs(main.build_scheduler())
    sweep = jobs["translation_sweep"]
    assert before + timedelta(seconds=50) <= sweep.next_run_time <= before + timedelta(seconds=70)
    assert sweep.trigger.interval == timedelta(minutes=15)
    assert f"Auto-translation on (model {settings.AUTO_TRANSLATE_MODEL})" in caplog.text


async def test_without_a_key_there_is_no_sweep_and_the_log_says_so(caplog):
    with caplog.at_level(logging.INFO, logger="app"):
        jobs = _jobs(main.build_scheduler())
    assert "translation_sweep" not in jobs
    assert any(r.levelno == logging.WARNING and "ANTHROPIC_API_KEY is not set" in r.getMessage() for r in caplog.records)


def test_app_logs_reach_the_service_log_at_info():
    app_logger = logging.getLogger("app")
    assert app_logger.level == logging.INFO
    assert sum(getattr(h, "_app_log", False) for h in app_logger.handlers) == 1
    main._app_logging()  # idempotent: a reload adds no second handler
    assert sum(getattr(h, "_app_log", False) for h in app_logger.handlers) == 1


async def test_with_background_jobs_off_the_app_starts_no_scheduler(monkeypatch, caplog):
    monkeypatch.setattr(settings, "BACKGROUND_JOBS", False)
    started = []
    monkeypatch.setattr(main, "build_scheduler", lambda: started.append(1))
    with caplog.at_level(logging.WARNING, logger="app"):
        async with main.lifespan(main.app):
            pass
    assert started == []
    assert "Background jobs off" in caplog.text
