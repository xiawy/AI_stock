"""APScheduler-based scheduler for the impact pipeline.

Runs the full pipeline daily at 00:00 / 08:30 / 12:30 / 14:30 (local) and
backs up the day's rankings at 23:30 (see ai_stock.pipeline.backup).
Follows the same pattern as the evolution scheduler: graceful degradation
if APScheduler is not installed, and the scheduler lives/dies with the host
process — jobs only fire while the backend server is running.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Optional

from .config import BACKUP_DAILY_AT, MAX_CONSECUTIVE_FAILURES, PIPELINE_SCHEDULE

logger = logging.getLogger(__name__)


def parse_schedule(entries: list) -> list[tuple[int, int]]:
    """Parse schedule entries into (hour, minute) pairs.

    Accepts "HH:MM" strings or ints (interpreted as whole hours).
    """
    slots: list[tuple[int, int]] = []
    for entry in entries:
        if isinstance(entry, str):
            hh, _, mm = entry.partition(":")
            slots.append((int(hh), int(mm or 0)))
        else:
            slots.append((int(entry), 0))
    return slots


class PipelineScheduler:
    """APScheduler wrapper for the impact assessment pipeline."""

    def __init__(
        self,
        config: dict,
        llm_quick: Optional[Any] = None,
        llm_deep: Optional[Any] = None,
    ) -> None:
        self._config = config
        self._llm_quick = llm_quick
        self._llm_deep = llm_deep
        self._scheduler = None
        self._consecutive_failures = 0
        # Guards against overlapping pipeline runs (manual + scheduled).
        self._run_lock = threading.Lock()

    @property
    def schedule_slots(self) -> list[tuple[int, int]]:
        """Resolved (hour, minute) run slots, e.g. [(8, 0), (14, 30)].

        Honors ``pipeline_schedule`` ("HH:MM" strings) and falls back to the
        legacy ``pipeline_hours`` ints, then to PIPELINE_SCHEDULE.
        """
        entries = self._config.get("pipeline_schedule")
        if not entries:
            entries = self._config.get("pipeline_hours", PIPELINE_SCHEDULE)
        return parse_schedule(entries)

    def set_llms(self, llm_quick: Any, llm_deep: Any) -> None:
        """Update the LLM instances (called when the pipeline service starts)."""
        self._llm_quick = llm_quick
        self._llm_deep = llm_deep

    def start(self) -> None:
        """Start the scheduler."""
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
        except ImportError:
            logger.warning(
                "APScheduler not installed. Pipeline scheduler disabled. "
                "Install with: pip install 'apscheduler>=3.10'"
            )
            return

        self._scheduler = BackgroundScheduler()

        slots = self.schedule_slots
        for hour, minute in slots:
            self._scheduler.add_job(
                self._run_pipeline,
                CronTrigger(hour=hour, minute=minute),
                id=f"pipeline_{hour:02d}{minute:02d}",
                name=f"Impact pipeline {hour:02d}:{minute:02d}",
                replace_existing=True,
                misfire_grace_time=3600,
            )

        # Daily ranking backup (新闻榜/行业榜/热股榜) after the last slot.
        backup_hour, backup_minute = BACKUP_DAILY_AT
        self._scheduler.add_job(
            self._run_backup,
            CronTrigger(hour=backup_hour, minute=backup_minute),
            id="pipeline_backup",
            name=f"Ranking backup {backup_hour:02d}:{backup_minute:02d}",
            replace_existing=True,
            misfire_grace_time=3600,
        )

        self._scheduler.start()
        logger.info(
            "Pipeline scheduler started: runs at %s, backup at %02d:%02d",
            ", ".join(f"{h:02d}:{m:02d}" for h, m in slots),
            backup_hour, backup_minute,
        )

    def stop(self) -> None:
        """Stop the scheduler."""
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            logger.info("Pipeline scheduler stopped")

    def trigger_manual(self) -> dict:
        """Manually trigger a pipeline run (for API endpoint)."""
        return self._run_pipeline()

    def _run_backup(self) -> dict:
        """Back up today's rankings; idempotent and never raises."""
        from .backup import backup_today_data

        try:
            return backup_today_data()
        except Exception as exc:
            logger.error("Ranking backup failed: %s", exc, exc_info=True)
            return {"status": "failed", "error": str(exc)}

    def ensure_today_backup(self) -> None:
        """Startup compensation for a missed backup slot.

        If the backend was not running at 23:30 and it is already past that
        slot with no backup file for today, kick one off now in a background
        thread. Before 23:30 the scheduled job owns the task.
        """
        now = datetime.now()
        if (now.hour, now.minute) < BACKUP_DAILY_AT:
            return

        from .backup import backup_exists_for_date

        today = now.strftime("%Y-%m-%d")
        try:
            if backup_exists_for_date(today):
                return
        except Exception as exc:
            logger.warning("Backup existence check failed (%s); trying anyway", exc)

        logger.info("No ranking backup for today %s; starting one now", today)
        threading.Thread(
            target=self._run_backup, name="ranking-backup", daemon=True,
        ).start()

    def _run_pipeline(self) -> dict:
        """Execute the full pipeline with failure tracking."""
        from .pipeline import run_full_pipeline

        if self._llm_quick is None or self._llm_deep is None:
            logger.error("Pipeline LLMs not configured; skipping run")
            return {"status": "failed", "error": "LLMs not configured"}

        if not self._run_lock.acquire(blocking=False):
            logger.warning("Pipeline already running; skipping duplicate trigger")
            return {"status": "skipped", "error": "Pipeline already running"}

        try:
            result = run_full_pipeline(
                self._config, self._llm_quick, self._llm_deep,
            )

            if result.get("status") == "completed":
                self._consecutive_failures = 0
                logger.info("Pipeline run succeeded")
            else:
                self._consecutive_failures += 1
                logger.warning(
                    "Pipeline run failed (%d consecutive failures)",
                    self._consecutive_failures,
                )

            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.error(
                    "ALERT: %d consecutive pipeline failures! "
                    "Check LLM connectivity and data source availability.",
                    self._consecutive_failures,
                )

            return result

        except Exception as exc:
            self._consecutive_failures += 1
            logger.error(
                "Pipeline exception (%d consecutive failures): %s",
                self._consecutive_failures, exc, exc_info=True,
            )
            return {"status": "failed", "error": str(exc)}
        finally:
            self._run_lock.release()

    @property
    def is_running(self) -> bool:
        """True while a pipeline run is executing (not the scheduler state)."""
        return self._run_lock.locked()

    @property
    def scheduler_active(self) -> bool:
        """True if the APScheduler background scheduler itself is started."""
        return self._scheduler is not None and self._scheduler.running

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures


# Module-level singleton
_scheduler: Optional[PipelineScheduler] = None


def get_scheduler() -> Optional[PipelineScheduler]:
    """Return the module-level scheduler singleton."""
    return _scheduler


def create_scheduler(
    config: dict,
    llm_quick: Any = None,
    llm_deep: Any = None,
) -> PipelineScheduler:
    """Create and return the scheduler singleton."""
    global _scheduler
    _scheduler = PipelineScheduler(config, llm_quick, llm_deep)
    return _scheduler
