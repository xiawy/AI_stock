"""Pipeline service — bridges FastAPI lifecycle with the ranking services.

重型新闻影响力评估流程已移除: 新闻榜改由 quant 选股流程 (MacroEventAgent)
抓完新闻后直接落库 (ai_stock.pipeline.news_board)。本服务只管理:
- APScheduler 生命周期 (每日榜单备份, 随 FastAPI lifespan 启停)
- 三榜 (新闻榜/行业榜/热股榜) 的读取入口
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class PipelineService:
    """Service layer wrapping the ranking read/backup layer for the backend."""

    def __init__(self) -> None:
        self._scheduler = None
        self._initialized = False

    def initialize(self, config: dict) -> None:
        """Start the ranking backup scheduler.

        Called during FastAPI lifespan startup.
        """
        if self._initialized:
            return

        try:
            from ai_stock.pipeline.scheduler import create_scheduler
            self._scheduler = create_scheduler(config)
            self._scheduler.start()

            self._initialized = True
            logger.info("Pipeline service initialized (backup scheduler)")

        except Exception as exc:
            logger.error("Pipeline service initialization failed: %s", exc)

    def shutdown(self) -> None:
        """Stop the scheduler. Called during FastAPI lifespan shutdown."""
        if self._scheduler:
            self._scheduler.stop()
            self._scheduler = None
        self._initialized = False
        logger.info("Pipeline service shut down")

    def ensure_today_backup(self) -> None:
        """Compensate for a missed 23:30 backup slot on startup.

        Delegates to the scheduler; no-op before the backup slot or when
        today's backup file already exists.
        """
        if not self._initialized or self._scheduler is None:
            return
        try:
            self._scheduler.ensure_today_backup()
        except Exception as exc:
            logger.warning("Today-backup check failed (non-fatal): %s", exc)

    def get_latest(self) -> Optional[dict]:
        """Get the latest pipeline results."""
        from ai_stock.pipeline.db_ops import get_latest_snapshot
        return get_latest_snapshot()

    def get_by_date(self, date_str: str) -> Optional[dict]:
        """Get pipeline results for a specific date."""
        from ai_stock.pipeline.db_ops import get_snapshot_by_date
        return get_snapshot_by_date(date_str)

    def get_industry_latest(self) -> Optional[dict]:
        """Get the latest industry board (行业榜).

        数据源 = quant 选股流程生命周期排序的前 10 行业 (含龙头股),
        存储于 quant_industry_board 表。
        """
        from ai_stock.quant.db_ops import get_latest_industry_board
        return get_latest_industry_board()

    def get_industry_by_date(self, date_str: str) -> Optional[dict]:
        """Get industry board for a specific date."""
        from ai_stock.quant.db_ops import get_industry_board_by_date
        return get_industry_board_by_date(date_str)

    def get_industry_news(self, board_id: int) -> Optional[dict]:
        """Get the news items related to one industry-board row."""
        from ai_stock.pipeline.db_ops import get_news_by_industry_ranking
        return get_news_by_industry_ranking(board_id)

    def get_hot_stocks_latest(self) -> dict:
        """热股榜 = 自选池活跃标的 (quant 选股流程产出).

        按 confidence 降序; 不再依赖 pipeline 的 stock_recommendations。
        """
        from datetime import timezone

        from ai_stock.quant.db_ops import get_optional_pool

        stocks = sorted(
            get_optional_pool("active"),
            key=lambda s: s.get("confidence") or 0.0,
            reverse=True,
        )
        return {
            "snapshot": {
                "source": "quant_optional_pool",
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            "recommendations": stocks,
        }

    def get_hot_stocks_by_date(self, date_str: str) -> dict:
        """按日期查询热股榜 = 自选池当日状态快照."""
        from ai_stock.quant.db_ops import get_optional_pool_as_of

        stocks = sorted(
            get_optional_pool_as_of(date_str),
            key=lambda s: s.get("confidence") or 0.0,
            reverse=True,
        )
        return {
            "snapshot": {
                "source": "quant_optional_pool",
                "as_of": date_str,
            },
            "recommendations": stocks,
        }


# Module-level singleton
_service: Optional[PipelineService] = None


def get_pipeline_service() -> PipelineService:
    """Return the singleton pipeline service."""
    global _service
    if _service is None:
        _service = PipelineService()
    return _service
