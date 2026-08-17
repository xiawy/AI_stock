"""Automatic data-retention cleanup.

Retention policy (see docs/README "功能" section):
- 诊股 (diagnosis) records — DB task rows + on-disk reports + resumable-task
  index — are kept for 20 days.
- 热股榜 / 新闻榜 / 行业榜 (ranking) snapshots — impact snapshots with their
  news items, industry rankings and recommendations — are kept for 70 days,
  together with their daily JSON backup files (backups/ next to the DB).

One pass runs at backend startup (background thread, non-blocking) and the
job repeats daily at 03:30 local time via APScheduler (between ranking
slots). Every step is individually fault-tolerant: a cleanup failure is
logged and never blocks startup or the API.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DIAGNOSIS_RETENTION_DAYS = 20
RANKING_RETENTION_DAYS = 70
CLEANUP_DAILY_AT = (3, 30)  # local time

_cleanup_scheduler = None


def _as_utc(dt: datetime) -> datetime:
    """SQLite DATETIME columns come back naive (values were written as UTC) —
    normalize to aware UTC for cutoff comparison."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def cleanup_diagnosis_data(
    max_age_days: int = DIAGNOSIS_RETENTION_DAYS,
) -> dict[str, int]:
    """Delete diagnosis records older than the retention window.

    Removes expired ``analysis_tasks`` rows plus the engine-written report
    files and resumable-task index entries for the same age.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    from app.core.database import SessionLocal
    from app.models.analysis_task import AnalysisTask

    removed_tasks = 0
    with SessionLocal() as session:
        expired = [
            row
            for row in session.query(AnalysisTask).all()
            if row.created_at and _as_utc(row.created_at) < cutoff
        ]
        for row in expired:
            session.delete(row)
        session.commit()
        removed_tasks = len(expired)

    files_cleaned: dict[str, int] = {"files": 0, "incomplete": 0}
    try:
        from web import history as web_history

        files_cleaned = web_history.cleanup_expired(max_age_days)
    except Exception as exc:
        logger.warning("Diagnosis file cleanup failed: %s", exc)

    result = {"tasks": removed_tasks, **files_cleaned}
    if any(result.values()):
        logger.info("Diagnosis cleanup (> %d days): %s", max_age_days, result)
    return result


def cleanup_ranking_snapshots(
    max_age_days: int = RANKING_RETENTION_DAYS,
) -> int:
    """Delete ranking snapshots older than the retention window.

    ORM-level cascade removes each snapshot's news items, industry
    rankings (行业榜) and stock recommendations along with it.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    from app.core.database import SessionLocal
    from ai_stock.pipeline.db_models import ImpactSnapshot

    removed = 0
    with SessionLocal() as session:
        for snap in session.query(ImpactSnapshot).all():
            ts = snap.snapshot_time or snap.created_at
            if ts and _as_utc(ts) < cutoff:
                session.delete(snap)
                removed += 1
        session.commit()

    if removed:
        logger.info(
            "Ranking cleanup (> %d days): removed %d snapshot(s)", max_age_days, removed
        )
    return removed


def run_all_cleanup() -> dict:
    """Run all cleanups; each is independently fault-tolerant."""
    stats: dict = {}
    try:
        stats["diagnosis"] = cleanup_diagnosis_data()
    except Exception as exc:
        logger.error("Diagnosis cleanup failed: %s", exc)
        stats["diagnosis"] = "error"
    try:
        stats["rankings"] = cleanup_ranking_snapshots()
    except Exception as exc:
        logger.error("Ranking cleanup failed: %s", exc)
        stats["rankings"] = "error"
    try:
        from ai_stock.pipeline.backup import cleanup_old_backups

        stats["backups"] = cleanup_old_backups(RANKING_RETENTION_DAYS)
    except Exception as exc:
        logger.error("Backup file cleanup failed: %s", exc)
        stats["backups"] = "error"
    return stats


def start_initial_cleanup() -> None:
    """Kick off one cleanup pass in a background thread (non-blocking)."""
    threading.Thread(target=run_all_cleanup, name="startup-cleanup", daemon=True).start()


def start_cleanup_scheduler() -> None:
    """Schedule the daily cleanup job. Idempotent; degrades gracefully when
    APScheduler is not installed (startup-only cleanup)."""
    global _cleanup_scheduler
    if _cleanup_scheduler is not None:
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning(
            "APScheduler not installed; data cleanup only runs at startup"
        )
        return

    hour, minute = CLEANUP_DAILY_AT
    sched = BackgroundScheduler()
    sched.add_job(
        run_all_cleanup,
        CronTrigger(hour=hour, minute=minute),
        id="daily_cleanup",
        name="Data retention cleanup",
        replace_existing=True,
    )
    sched.start()
    _cleanup_scheduler = sched
    logger.info("Cleanup scheduler started: daily at %02d:%02d", hour, minute)


def stop_cleanup_scheduler() -> None:
    global _cleanup_scheduler
    if _cleanup_scheduler is not None:
        _cleanup_scheduler.shutdown(wait=False)
        _cleanup_scheduler = None
        logger.info("Cleanup scheduler stopped")
