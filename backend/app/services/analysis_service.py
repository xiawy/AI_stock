"""Analysis task manager bridging the original web-era pipeline.

Reuses (unchanged) from the original project:
- ``web.runner.run_analysis_in_thread`` — daemon-thread pipeline execution
- ``web.progress.ProgressTracker`` — thread-safe mutable progress state
- ``web.history`` — resumable-task bookkeeping
- ``web.stock_display`` — "code + name" labels

New here: task_id registry, per-request config assembly (formerly the web UI's
``st.session_state``), and DB persistence via ``AnalysisTask`` rows.

A5（任务状态表内化）：``ProgressTracker`` 仍是进程内对象（执行体），但所有
对外的状态都来自 ``analysis_tasks`` 行 —— 见 ``analysis_state``。于是
- 任意 worker 都能 ``snapshot`` / ``result`` 任意任务（不再 404）；
- pause/resume/stop 跨进程通过 ``control`` 列下发；
- 心跳超时才回收孤儿行，``uvicorn --workers N`` 不再互杀。

Engine imports are lazy so this module (and the whole API) can load without
the heavy analysis stack installed.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.trading import bootstrap as bootstrap_engine
from app.models import AnalysisTask
from app.services import analysis_state

logger = logging.getLogger(__name__)


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
    """Registry of live trackers + DB-backed state (A5).

    ``_tasks`` 只保存**本进程**正在执行的任务（执行体），对外可见的状态一律
    以 ``analysis_tasks`` 行为准，因此多 worker 下任意实例都能回答任意任务。
    """

    def __init__(self) -> None:
        self._tasks: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._owner = analysis_state.owner_id()
        self._flushed: dict[str, str] = {}
        self._hb_thread: threading.Thread | None = None
        self._hb_stop = threading.Event()

    # ── Session helpers ──────────────────────────────────────────────────

    @staticmethod
    def _session():
        """独立短事务 session（延迟导入，便于测试替换 SessionLocal）。"""
        from app.core.database import SessionLocal

        return SessionLocal()

    def _local(self, task_id: str):
        with self._lock:
            return self._tasks.get(task_id)

    def _load_row(self, task_id: str) -> AnalysisTask | None:
        with self._session() as db:
            return db.get(AnalysisTask, task_id)

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
                owner=self._owner,
            )
        )
        db.commit()

        # 首帧进度立刻落库：其它 worker 从这一刻起就能读到该任务（而非等到第一
        # 个心跳周期），也避免"行存在但 progress 为空"被误判为孤儿。
        self._flush(task_id, tracker, force=True)
        self.start_heartbeat()
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

        # deepcopy（O4）：浅拷贝下嵌套 dict（role_llms/tool_vendors…）与全局
        # 共享，多任务并发时一次定制会泄漏进其它任务。
        import copy

        config = copy.deepcopy(DEFAULT_CONFIG)
        config["data_vendors"] = {
            "core_stock_apis": "a_stock",
            "technical_indicators": "a_stock",
            "fundamental_data": "a_stock",
            "news_data": "a_stock",
            "signal_data": "a_stock",
        }
        # 行业榜联动（宏观→中观→微观）：把最新行业热度榜注入诊股上下文，
        # 分析师可结合个股所处行业的β环境研判。无数据时静默跳过。
        config["industry_context"] = AnalysisTaskManager._build_industry_context()
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

    @staticmethod
    def _build_industry_context() -> dict[str, str]:
        """Summarize the latest industry board (行业榜) for analyst prompts.

        行业榜由 quant 选股流程产出 (生命周期排序前 10 行业, 含龙头股).
        Returns {"heatmap": str, "hot_sector_stocks": str}; both empty when
        the industry board is unavailable or empty.
        """
        try:
            from ai_stock.quant.db_ops import get_latest_industry_board

            data = get_latest_industry_board()
        except Exception as exc:  # engine missing / table absent — degrade silently
            logging.getLogger(__name__).debug(
                "industry context unavailable: %s", exc
            )
            return {"heatmap": "", "hot_sector_stocks": ""}

        rankings = (data or {}).get("rankings") or []
        if not rankings:
            return {"heatmap": "", "hot_sector_stocks": ""}

        lines = [f"榜单日期：{data.get('rank_date', '')}（quant 选股生命周期排序）"]
        for row in rankings[:10]:
            inflow = row.get("main_net_inflow")
            inflow_txt = (
                f"主力净流入{inflow / 1e8:+.1f}亿" if isinstance(inflow, (int, float))
                else "资金数据缺失"
            )
            lines.append(
                f"{row.get('rank')}. {row.get('industry')} 阶段{row.get('stage', '')} "
                f"优选级{row.get('heat_score', 0)} {inflow_txt}"
            )

        hot_stocks: list[str] = []
        for row in rankings[:3]:
            for stock in row.get("leader_stocks") or []:
                name = stock.get("name") or stock.get("code")
                if name:
                    hot_stocks.append(f"{name}({row.get('industry')})")

        return {
            "heatmap": "\n".join(lines),
            "hot_sector_stocks": "、".join(hot_stocks[:15]),
        }

    # ── Registry access ──────────────────────────────────────────────────

    def get_tracker(self, task_id: str):
        """仅返回**本进程**持有的执行体；不存在时返回 None（不再直接 404）。

        跨 worker 的场景下"没有 tracker"是正常状态，调用方应回落到 DB 状态。
        """
        return self._local(task_id)

    def snapshot(self, task_id: str) -> dict[str, Any]:
        """任务进度。执行体在本进程 → 实时；否则 → 读表里的进度快照。"""
        tracker = self._local(task_id)
        if tracker is not None:
            payload = self._build_snapshot(task_id, tracker)
            payload["state_source"] = "live"
            return payload

        row = self._load_row(task_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"任务不存在: {task_id}",
            )
        return self._overlay_row(row.progress(), row, source="snapshot")

    def result(self, task_id: str) -> dict[str, Any]:
        tracker = self._local(task_id)
        if tracker is not None:
            if not tracker.is_complete:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="分析尚未完成" if tracker.is_running else "分析未成功完成，无结果可取",
                )
            payload = self._build_result(task_id, tracker)
            payload["state_source"] = "live"
            return payload

        row = self._load_row(task_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"任务不存在: {task_id}",
            )
        if row.status != "completed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="分析尚未完成" if row.status in ("running", "paused") else "分析未成功完成，无结果可取",
            )
        payload = analysis_state.loads(row.result_json, {})
        if not payload.get("final_state"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该任务由旧版本创建，结果未持久化，无法在此进程读取",
            )
        payload.setdefault("task_id", row.id)
        payload.setdefault("ticker", row.ticker)
        payload.setdefault("trade_date", row.trade_date)
        payload.setdefault("signal", row.signal)
        payload["state_source"] = "snapshot"
        return payload

    # ── Lifecycle control ────────────────────────────────────────────────

    def pause(self, task_id: str) -> bool:
        tracker = self._local(task_id)
        if tracker is not None:
            if not tracker.pause():
                return False
            _engine().history.record_incomplete_task(
                tracker.ticker,
                tracker.trade_date,
                status="paused",
                completed_stages=tracker.completed_stages,
            )
            self._flush(task_id, tracker, force=True)
            return True
        return self._remote_control(task_id, analysis_state.CONTROL_PAUSE, ("running",))

    def resume(self, task_id: str) -> bool:
        tracker = self._local(task_id)
        if tracker is not None:
            if not tracker.resume():
                return False
            _engine().history.record_incomplete_task(
                tracker.ticker,
                tracker.trade_date,
                status="running",
                completed_stages=tracker.completed_stages,
            )
            self._flush(task_id, tracker, force=True)
            return True
        return self._remote_control(task_id, analysis_state.CONTROL_RESUME, ("paused",))

    def stop(self, task_id: str) -> bool:
        tracker = self._local(task_id)
        if tracker is None:
            return self._remote_control(
                task_id, analysis_state.CONTROL_STOP, ("running", "paused"),
            )
        self._stop_tracker(tracker)
        self._flush(task_id, tracker, force=True)
        return True

    def _stop_tracker(self, tracker) -> None:
        if tracker.is_running:
            tracker.request_stop()
            _engine().history.clear_incomplete_task(tracker.ticker, tracker.trade_date)
        else:
            tracker.mark_stopped()

    def _remote_control(self, task_id: str, control: str, allowed: tuple[str, ...]) -> bool:
        """把控制指令写到表里，交给持有执行体的进程消费。"""
        with self._session() as db:
            row = db.get(AnalysisTask, task_id)
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"任务不存在: {task_id}",
                )
            if row.status not in allowed:
                return False
            return analysis_state.set_control(db, task_id, control)

    # ── DB sync (called when rows are read) ──────────────────────────────

    def sync_task_row(self, db: Session, row: AnalysisTask) -> None:
        """Refresh a DB row from its live tracker, if one exists locally."""
        tracker = self._local(row.id)
        if tracker is None:
            return
        self._flush(row.id, tracker)
        db.refresh(row)  # 进度由独立短事务写入，刷新让调用方看到最新值

    # ── Persistence internals (A5) ───────────────────────────────────────

    @staticmethod
    def _status_of(tracker) -> str:
        if tracker.is_complete:
            return "completed"
        if tracker.error:
            return "error"
        if tracker.stop_requested or (
            not tracker.is_running and not tracker.is_complete and not tracker.error
        ):
            return "stopped"
        if tracker.is_paused:
            return "paused"
        return "running"

    def _build_snapshot(self, task_id: str, tracker) -> dict[str, Any]:
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

    def _build_result(self, task_id: str, tracker) -> dict[str, Any]:
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

    @staticmethod
    def _digest(payload: dict[str, Any]) -> str:
        """进度指纹：内容不变时只刷心跳，不重写整段 JSON。"""
        raw = analysis_state.dumps(payload).encode("utf-8", "ignore")
        return hashlib.sha1(raw).hexdigest()

    def _flush(self, task_id: str, tracker, *, force: bool = False) -> None:
        payload = self._build_snapshot(task_id, tracker)
        digest = self._digest(payload)
        status_value = self._status_of(tracker)
        result_payload = (
            self._build_result(task_id, tracker) if tracker.is_complete else None
        )
        try:
            with self._session() as db:
                if digest == self._flushed.get(task_id) and not force:
                    analysis_state.touch_heartbeat(db, [task_id], self._owner)
                    return
                analysis_state.persist_progress(
                    db,
                    task_id,
                    payload,
                    status=status_value,
                    owner=self._owner,
                    error=tracker.error or "",
                    signal=tracker.signal if tracker.is_complete else None,
                    result=result_payload,
                )
        except Exception as exc:  # 持久化失败不能打断分析主流程
            logger.warning("task state persist failed for %s: %s", task_id, exc)
            return
        self._flushed[task_id] = digest

    # ── Heartbeat / reaping / cross-process control ───────────────────────

    def start_heartbeat(self) -> None:
        """启动心跳线程（幂等）。无本地任务时它就是低频的孤儿巡检。"""
        if self._hb_thread is not None and self._hb_thread.is_alive():
            return
        self._hb_stop.clear()
        thread = threading.Thread(
            target=self._heartbeat_loop,
            name="analysis-task-heartbeat",
            daemon=True,
        )
        self._hb_thread = thread
        thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._hb_stop.wait(analysis_state.HEARTBEAT_INTERVAL_SECONDS):
            try:
                self.flush_local()
                self.reap_stale()
            except Exception as exc:  # pragma: no cover - 线程不能因单次失败退出
                logger.warning("analysis task heartbeat cycle failed: %s", exc)

    def flush_local(self) -> int:
        """落库本进程所有执行体，并消费跨进程控制指令。返回处理的任务数。"""
        with self._lock:
            items = list(self._tasks.items())
        if not items:
            return 0
        task_ids = [task_id for task_id, _ in items]
        try:
            with self._session() as db:
                pending = dict(analysis_state.load_controls(db, task_ids))
        except Exception as exc:  # pragma: no cover
            logger.warning("control poll failed: %s", exc)
            pending = {}
        for task_id, tracker in items:
            control = pending.get(task_id)
            if control:
                self._apply_control(task_id, tracker, control)
            self._flush(task_id, tracker)
        return len(items)

    def _apply_control(self, task_id: str, tracker, control: str) -> None:
        """把表里的控制指令落到本地执行体，然后清空该指令。"""
        try:
            if control == analysis_state.CONTROL_PAUSE:
                tracker.pause()
            elif control == analysis_state.CONTROL_RESUME:
                tracker.resume()
            elif control == analysis_state.CONTROL_STOP:
                self._stop_tracker(tracker)
            else:
                logger.warning("unknown task control %r for %s", control, task_id)
        finally:
            try:
                with self._session() as db:
                    analysis_state.clear_control(db, task_id, control)
            except Exception as exc:  # pragma: no cover
                logger.warning("clear control failed for %s: %s", task_id, exc)

    def reap_stale(self, stale_after: int = analysis_state.STALE_AFTER_SECONDS) -> int:
        """回收心跳超时的 running/paused 行；本进程活着的任务心跳新鲜，不受影响。"""
        try:
            with self._session() as db:
                count = analysis_state.reap_stale_tasks(db, stale_after=stale_after)
        except Exception as exc:  # pragma: no cover
            logger.warning("stale task reap failed: %s", exc)
            return 0
        if count:
            logger.warning(
                "Reaped %d analysis task(s) whose executor heartbeat expired", count
            )
        return count

    @staticmethod
    def _overlay_row(
        payload: dict[str, Any], row: AnalysisTask, *, source: str,
    ) -> dict[str, Any]:
        """以表里的状态为准覆盖快照，避免旧进度声称"仍在运行"。"""
        data = dict(payload or {})
        data["task_id"] = row.id
        data["ticker"] = data.get("ticker") or row.ticker
        data["trade_date"] = data.get("trade_date") or row.trade_date
        data["is_running"] = row.status == "running"
        data["is_complete"] = row.status == "completed"
        data["is_paused"] = row.status == "paused"
        data["stop_requested"] = bool(data.get("stop_requested")) or row.status == "stopped"
        data.setdefault("error", None)
        if row.error:
            data["error"] = row.error
        data.setdefault("signal", row.signal)
        data.setdefault("elapsed", 0.0)
        data.setdefault("current_stage", "")
        data.setdefault("stages", [])
        data.setdefault("llm_calls", 0)
        data.setdefault("tool_calls", 0)
        data.setdefault("tokens_in", 0)
        data.setdefault("tokens_out", 0)
        data["state_source"] = source
        return data

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
