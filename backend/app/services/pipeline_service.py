"""Pipeline service — bridges FastAPI lifecycle with the ranking services.

重型新闻影响力评估流程已移除: 新闻榜改由 quant 选股流程 (MacroEventAgent)
抓完新闻后直接落库 (ai_stock.pipeline.news_board)。本服务只管理:
- APScheduler 生命周期 (每日榜单备份, 随 FastAPI lifespan 启停)
- 三榜 (新闻榜/行业榜/热股榜) 的读取入口
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _latest_pool_date() -> str:
    """最近一个有池快照的交易日 (没写过快照时退回今天)。"""
    from datetime import date

    from ai_stock.quant.db_ops import get_previous_pool_snapshot_date

    return get_previous_pool_snapshot_date("9999-12-31") or date.today().isoformat()


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
        """热股榜 = 自选池活跃标的 (quant 选股流程产出)。

        排序按 ``dyn_confidence``(动态置信度), 与 ``buy.py`` 的候选排队口径
        一致; 入池时的静态 ``confidence`` 并列展示但**不再决定名次** ——
        此前按静态分排, 用户看到的第 1 名可能正是系统这轮准备踢掉的票。
        """
        from ai_stock.quant.db_ops import get_optional_pool

        from .board_view import annotate_pool

        stocks = annotate_pool(get_optional_pool("active"))
        return {
            "snapshot": {
                "source": "quant_optional_pool",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "sort_by": "dyn_confidence",
                "count": len(stocks),
            },
            "recommendations": stocks,
        }

    def get_hot_stocks_by_date(self, date_str: str) -> dict:
        """按日期查询热股榜。

        优先读 ``quant_pool_snapshot``(当日真实快照, 可复现); 没有快照时退回
        "倒推存在性 + 当前分数"的近似视图, 并显式标 ``approximate=True`` ——
        近似视图每查一次结果都会变, 不能让用户以为那是历史。
        """
        from ai_stock.quant.db_ops import get_optional_pool_as_of, get_pool_snapshot

        from .board_view import annotate_pool

        exact = get_pool_snapshot(date_str)
        if exact:
            stocks = annotate_pool(exact)
            return {
                "snapshot": {
                    "source": "quant_pool_snapshot",
                    "as_of": date_str,
                    "sort_by": "dyn_confidence",
                    "count": len(stocks),
                    "approximate": False,
                },
                "recommendations": stocks,
            }

        stocks = annotate_pool(get_optional_pool_as_of(date_str))
        return {
            "snapshot": {
                "source": "quant_optional_pool",
                "as_of": date_str,
                "sort_by": "dyn_confidence",
                "count": len(stocks),
                "approximate": True,
            },
            "recommendations": stocks,
        }

    # ---- 跨期对比 (变化视图) ---------------------------------------------

    def get_hot_stocks_diff(self, date_str: str = "") -> dict:
        """热股榜变化: 今天新进哪些、掉出哪些、名次变动最大的是谁。"""
        from ai_stock.quant.db_ops import (
            get_previous_pool_snapshot_date,
            get_pool_snapshot,
        )

        from .board_view import annotate_pool, diff_ranked, no_previous_diff

        current_date = date_str or _latest_pool_date()
        current = annotate_pool(get_pool_snapshot(current_date)) if current_date else []
        if not current:
            return {"date": current_date, "previous_date": "", **no_previous_diff([])}

        prev_date = get_previous_pool_snapshot_date(current_date)
        if not prev_date:
            return {"date": current_date, "previous_date": "", **no_previous_diff(current)}

        previous = annotate_pool(get_pool_snapshot(prev_date))
        result = diff_ranked(current, previous, key="symbol")
        return {"date": current_date, "previous_date": prev_date, **result}

    def get_industry_diff(self, date_str: str = "") -> dict:
        """行业榜变化: Δrank / 今日新进 / 连榜天数。"""
        from datetime import date, timedelta

        from ai_stock.quant.db_ops import get_industry_boards_since

        from .board_view import consecutive_days, diff_ranked, no_previous_diff

        start = (
            date.fromisoformat(date_str) - timedelta(days=30)
            if date_str
            else date.today() - timedelta(days=30)
        ).isoformat()
        history = get_industry_boards_since(start)
        if date_str:
            history = [h for h in history if h["rank_date"] <= date_str]
        if not history:
            return {"date": date_str, "previous_date": "", **no_previous_diff([])}

        current = history[0]
        rows = current["rows"]
        if len(history) <= 1:
            return {
                "date": current["rank_date"],
                "previous_date": "",
                **no_previous_diff(rows),
            }

        result = diff_ranked(rows, history[1]["rows"], key="industry")
        for row in result["rows"]:
            row["days_on_board"] = consecutive_days(
                history, str(row.get("industry") or ""),
            )
        return {
            "date": current["rank_date"],
            "previous_date": history[1]["rank_date"],
            **result,
        }

    def get_impact_diff(self, date_str: str = "") -> dict:
        """新闻榜变化: 与**上一份快照**对比(不是昨天 —— 一天有 3 份)。"""
        from ai_stock.pipeline.db_ops import get_previous_snapshot, get_ranking_map

        from .board_view import diff_ranked, no_previous_diff

        current = (
            self.get_by_date(date_str) if date_str else self.get_latest()
        ) or {}
        items = current.get("news_items") or []
        snapshot = current.get("snapshot") or {}
        if not items:
            return {"date": date_str, "previous_date": "", **no_previous_diff([])}

        prev = get_previous_snapshot()
        if not prev:
            return {"date": snapshot.get("snapshot_time", date_str),
                    "previous_date": "", **no_previous_diff(items)}

        prev_ranks = get_ranking_map(prev["id"])
        stamped = [
            {**item, "key": item.get("cluster_id") or item.get("title") or ""}
            for item in items
        ]
        previous = [
            {"key": k, "title": v.get("title", "")}
            for k, v in sorted(prev_ranks.items(), key=lambda kv: kv[1].get("rank") or 0)
        ]
        result = diff_ranked(stamped, previous, key="key")
        return {
            "date": snapshot.get("snapshot_time") or date_str,
            "previous_date": prev.get("snapshot_time") or "",
            **result,
        }

    def get_cluster_members(self, snapshot_id: int, cluster_id: str) -> list[dict]:
        """同事件多源成员(新闻榜"一行一件事"的展开明细)。"""
        from ai_stock.pipeline.db_ops import get_cluster_members

        if not cluster_id:
            return []
        return get_cluster_members(snapshot_id, cluster_id)


# Module-level singleton
_service: Optional[PipelineService] = None


def get_pipeline_service() -> PipelineService:
    """Return the singleton pipeline service."""
    global _service
    if _service is None:
        _service = PipelineService()
    return _service
