"""Dedicated scheduler_worker process; never start APScheduler inside API workers."""

from __future__ import annotations

import logging
from threading import Event

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import get_settings
from app.services.automation import run_monitoring_rules
from app.services.production import run_due_refresh_jobs

settings = get_settings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ai-analytics-scheduler")


def main() -> None:
    if not settings.scheduler_enabled:
        logger.info("Scheduler disabled")
        return

    scheduler = BackgroundScheduler(daemon=False)
    scheduler.add_job(
        run_due_refresh_jobs,
        "interval",
        minutes=settings.refresh_interval_minutes,
        id="refresh-jobs",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_monitoring_rules,
        "interval",
        minutes=settings.monitoring_interval_minutes,
        id="monitoring-rules",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info(
        "Dedicated scheduler started refresh=%sm monitoring=%sm",
        settings.refresh_interval_minutes,
        settings.monitoring_interval_minutes,
    )
    stop = Event()
    try:
        stop.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown(wait=True)
        logger.info("Dedicated scheduler stopped")


if __name__ == "__main__":
    main()
