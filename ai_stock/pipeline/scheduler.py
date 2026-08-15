"""APScheduler-based scheduler for the impact pipeline.

Runs the full pipeline at 08:00 and 20:00 daily.  Follows the same pattern
as the evolution scheduler: graceful degradation if APScheduler is not
installed, and the scheduler lives/dies with the host process.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from .config import MAX_CONSECUTIVE_FAILURES, PIPELINE_SCHEDULE

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
            )

        self._scheduler.start()
        logger.info(
            "Pipeline scheduler started: runs at %s",
            ", ".join(f"{h:02d}:{m:02d}" for h, m in slots),
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
