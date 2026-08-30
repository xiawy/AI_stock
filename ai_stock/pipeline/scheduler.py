"""APScheduler-based scheduler for the ranking backup.

重型新闻影响力评估流程已移除: 新闻榜改由 quant 选股流程 (MacroEventAgent)
抓完新闻后直接落库 (见 ai_stock.pipeline.news_board)。本调度器只剩每日
23:30 的榜单备份任务 (见 ai_stock.pipeline.backup)。

Follows the same pattern as the evolution scheduler: graceful degradation
if APScheduler is not installed, and the scheduler lives/dies with the host
process — jobs only fire while the backend server is running.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

from .config import BACKUP_DAILY_AT

logger = logging.getLogger(__name__)


class PipelineScheduler:
    """APScheduler wrapper for the daily ranking backup."""

    def __init__(self, config: dict) -> None:
        self._config = config
        self._scheduler = None

    def start(self) -> None:
        """Start the scheduler."""
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
        except ImportError:
            logger.warning(
                "APScheduler not installed. Ranking backup scheduler disabled. "
                "Install with: pip install 'apscheduler>=3.10'"
            )
            return

        self._scheduler = BackgroundScheduler()

        # Daily ranking backup (新闻榜/行业榜/热股榜).
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
            "Ranking backup scheduler started: backup at %02d:%02d",
            backup_hour, backup_minute,
        )

    def stop(self) -> None:
        """Stop the scheduler."""
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            logger.info("Ranking backup scheduler stopped")

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

    @property
    def scheduler_active(self) -> bool:
        """True if the APScheduler background scheduler itself is started."""
        return self._scheduler is not None and self._scheduler.running


# Module-level singleton
_scheduler: Optional[PipelineScheduler] = None


def get_scheduler() -> Optional[PipelineScheduler]:
    """Return the module-level scheduler singleton."""
    return _scheduler


def create_scheduler(config: dict) -> PipelineScheduler:
    """Create and return the scheduler singleton."""
    global _scheduler
    _scheduler = PipelineScheduler(config)
    return _scheduler
