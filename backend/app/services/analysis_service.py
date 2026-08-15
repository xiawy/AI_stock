"""Analysis task manager bridging the original web-era pipeline.

Reuses (unchanged) from the original project:
- ``web.runner.run_analysis_in_thread`` — daemon-thread pipeline execution
- ``web.progress.ProgressTracker`` — thread-safe mutable progress state
- ``web.history`` — resumable-task bookkeeping
- ``web.stock_display`` — "code + name" labels

New here: task_id registry, per-request config assembly (formerly the web UI's
``st.session_state``), and DB persistence via ``AnalysisTask`` rows.

Engine imports are lazy so this module (and the whole API) can load without
the heavy analysis stack installed.
"""

from __future__ import annotations

import threading
import uuid
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.trading import bootstrap as bootstrap_engine
from app.models import AnalysisTask


def _engine() -> SimpleNamespace:
    """Import the original project's runner/progress/history (cached by sys.modules)."""
    bootstrap_engine()
    from web import history as web_history
    from web import progress as web_progress
    from web import runner as web_runner
    from web import stock_display as web_stock_display

    return SimpleNamespace(
        history=web_history,
        progress=web_progress,
        runner=web_runner,
        stock_display=web_stock_display,
    )


class AnalysisTaskManager:
    """In-memory registry of live trackers + DB synchronization on demand."""

    def __init__(self) -> None:
        self._tasks: dict[str, ProgressTracker] = {}
        self._lock = threading.Lock()

    # ── Start / resume ───────────────────────────────────────────────────

    def start(
        self,
        db: Session,
        user_id: int,
        ticker: str,
        trade_date: str,
        *,
        lookback_days: int | None = None,
        fresh: bool = True,
    ) -> str:
        eng = _engine()
        from ai_stock.dataflows.a_stock import resolve_ticker

        # Resolve Chinese names / prefixed symbols to a plain 6-digit code.
        try:
            code = resolve_ticker(ticker)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

        if fresh:
            from ai_stock.default_config import DEFAULT_CONFIG
            from ai_stock.graph.checkpointer import clear_checkpoint

            eng.history.clear_incomplete_task(code, trade_date)
            clear_checkpoint(DEFAULT_CONFIG["data_cache_dir"], code, trade_date)

        config = self.build_config(lookback_days=lookback_days)

        tracker = eng.progress.ProgressTracker(ticker=code, trade_date=trade_date)
        eng.runner.run_analysis_in_thread(
            ticker=code,
            trade_date=trade_date,
            config=config,
            tracker=tracker,
        )

        task_id = uuid.uuid4().hex
        with self._lock:
            self._tasks[task_id] = tracker

        db.add(
            AnalysisTask(
                id=task_id,
                user_id=user_id,
                ticker=code,
                stock_name=eng.stock_display.resolve_stock_name(code) or "",
                trade_date=trade_date,
                status="running",
                llm_provider=config["llm_provider"],
                quick_think_llm=config["quick_think_llm"],
                deep_think_llm=config["deep_think_llm"],
                lookback_days=config["market_lookback_days"],
            )
        )
        db.commit()
        return task_id

    @staticmethod
    def build_config(
        *,
        lookback_days: int | None = None,
    ) -> dict[str, Any]:
        """Assemble the pipeline config.

        LLM settings (provider / model / backend_url) come from DEFAULT_CONFIG
        which reads them from .env — no per-request override.
        """
        from ai_stock.default_config import DEFAULT_CONFIG

        config = DEFAULT_CONFIG.copy()
        config["data_vendors"] = {
            "core_stock_apis": "a_stock",
            "technical_indicators": "a_stock",
            "fundamental_data": "a_stock",
            "news_data": "a_stock",
            "signal_data": "a_stock",
        }
        if lookback_days is None:
            # Default: analysis window = first day of trade_date's month.
            from datetime import date, timedelta

            end = date.fromisoformat(_today_iso())
            start = end.replace(day=1)
            lookback_days = max((end - start).days, 5)
        config["market_lookback_days"] = max(int(lookback_days), 5)
        config["max_debate_rounds"] = 1
        config["max_risk_discuss_rounds"] = 1
        config["checkpoint_enabled"] = True
        config["output_language"] = "Chinese"
        return config

    # ── Registry access ──────────────────────────────────────────────────

    def get_tracker(self, task_id: str):
        with self._lock:
            tracker = self._tasks.get(task_id)
        if tracker is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"任务不存在或已随服务重启丢失: {task_id}",
            )
        return tracker

    def snapshot(self, task_id: str) -> dict[str, Any]:
        tracker = self.get_tracker(task_id)
        stages = [
            {
                "id": s["id"],
                "name": s["name"],
                "icon": s["icon"],
                "status": tracker.stage_status(s["id"]),
                "report": tracker.stage_reports.get(s["id"], ""),
            }
            for s in _engine().progress.PIPELINE_STAGES
        ]
        return {
            "task_id": task_id,
            "ticker": tracker.ticker,
            "trade_date": tracker.trade_date,
            "is_running": tracker.is_running,
            "is_complete": tracker.is_complete,
            "is_paused": tracker.is_paused,
            "stop_requested": tracker.stop_requested,
            "error": tracker.error,
            "signal": tracker.signal,
            "elapsed": tracker.elapsed,
            "current_stage": tracker.current_stage,
            "stages": stages,
            "llm_calls": tracker.llm_calls,
            "tool_calls": tracker.tool_calls,
            "tokens_in": tracker.tokens_in,
            "tokens_out": tracker.tokens_out,
        }

    def result(self, task_id: str) -> dict[str, Any]:
        tracker = self.get_tracker(task_id)
        if not tracker.is_complete:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="分析尚未完成" if tracker.is_running else "分析未成功完成，无结果可取",
            )
        label = _engine().stock_display.stock_display_label(
            tracker.ticker, tracker.final_state
        )
        return {
            "task_id": task_id,
            "ticker": tracker.ticker,
            "stock_label": label,
            "trade_date": tracker.trade_date,
            "signal": tracker.signal,
            "elapsed": tracker.elapsed,
            "final_state": tracker.final_state,
        }

    # ── Lifecycle control ────────────────────────────────────────────────

    def pause(self, task_id: str) -> bool:
        tracker = self.get_tracker(task_id)
        ok = tracker.pause()
        if ok:
            _engine().history.record_incomplete_task(
                tracker.ticker,
                tracker.trade_date,
                status="paused",
                completed_stages=tracker.completed_stages,
            )
        return ok

    def resume(self, task_id: str) -> bool:
        tracker = self.get_tracker(task_id)
        ok = tracker.resume()
        if ok:
            _engine().history.record_incomplete_task(
                tracker.ticker,
                tracker.trade_date,
                status="running",
                completed_stages=tracker.completed_stages,
            )
        return ok

    def stop(self, task_id: str) -> bool:
        tracker = self.get_tracker(task_id)
        was_running = tracker.is_running
        if was_running:
            tracker.request_stop()
            _engine().history.clear_incomplete_task(tracker.ticker, tracker.trade_date)
        else:
            tracker.mark_stopped()
        return True

    # ── DB sync (called when rows are read) ──────────────────────────────

    def sync_task_row(self, db: Session, row: AnalysisTask) -> None:
        """Refresh a DB row from its live tracker, if one exists."""
        with self._lock:
            tracker = self._tasks.get(row.id)
        if tracker is None:
            return
        status_value = "running"
        if tracker.is_complete:
            status_value = "completed"
        elif tracker.error:
            status_value = "error"
        elif tracker.stop_requested or (
            not tracker.is_running and not tracker.is_complete and not tracker.error
        ):
            status_value = "stopped"
        elif tracker.is_paused:
            status_value = "paused"

        changed = (
            row.status != status_value
            or (tracker.is_complete and row.signal != tracker.signal)
            or (tracker.error and row.error != tracker.error)
        )
        if changed:
            row.status = status_value
            row.signal = tracker.signal if tracker.is_complete else row.signal
            row.error = tracker.error or ""
            db.commit()

    # ── Resumable tasks (cross-restart, file-system based) ────────────────

    @staticmethod
    def incomplete_tasks() -> list[dict[str, Any]]:
        return _engine().history.get_incomplete_history()


def _today_iso() -> str:
    """A-share market date (Asia/Shanghai) as YYYY-MM-DD."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


task_manager = AnalysisTaskManager()
