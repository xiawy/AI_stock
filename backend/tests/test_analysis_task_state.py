"""A5 regression: 任务状态表内化（任意 worker 都能服务任意任务）。

覆盖的契约：
1. 执行体不在本进程时，``snapshot`` / ``result`` 从表里的进度快照返回（不再 404）；
2. 表里的 ``status`` 优先于旧快照，避免"心跳已死却仍报 running"；
3. pause / resume / stop 跨进程走 ``control`` 列，由持有执行体的进程消费并清空；
4. 孤儿的判定依据是心跳超时，**心跳新鲜的 running 行不会被误杀**（多 worker 前提）；
5. 进度落库后，列表接口读到的行状态是新鲜的。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.core.database as database_module
import app.models  # noqa: F401 — register all tables on Base.metadata
from app.core.database import Base
from app.models.analysis_task import AnalysisTask
from app.models.user import User
from app.services import analysis_service, analysis_state
from web.progress import PIPELINE_STAGES


class _ProgressNS:
    PIPELINE_STAGES = PIPELINE_STAGES


def _fake_engine() -> SimpleNamespace:
    """替掉重量级分析引擎：只要 progress / stock_display / history 三件。"""
    return SimpleNamespace(
        progress=_ProgressNS,
        stock_display=SimpleNamespace(
            stock_display_label=lambda code, state: f"{code} 标签",
        ),
        history=SimpleNamespace(
            record_incomplete_task=lambda *a, **k: None,
            clear_incomplete_task=lambda *a, **k: None,
        ),
    )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'task_state.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr(database_module, "SessionLocal", session_factory)
    monkeypatch.setattr(analysis_service, "_engine", _fake_engine)

    manager = analysis_service.AnalysisTaskManager()
    with session_factory() as session:
        user = User(
            username=f"u{uuid.uuid4().hex[:8]}",
            email=f"{uuid.uuid4().hex[:8]}@example.com",
            password_hash="x",
        )
        session.add(user)
        session.commit()
        user_id = user.id

    return SimpleNamespace(
        session_factory=session_factory, manager=manager, user_id=user_id,
    )


def _add_task(
    env,
    *,
    status: str = "running",
    progress: dict | None = None,
    result: dict | None = None,
    heartbeat: datetime | None = None,
    control: str = "",
    error: str = "",
) -> str:
    task_id = uuid.uuid4().hex
    with env.session_factory() as session:
        session.add(
            AnalysisTask(
                id=task_id,
                user_id=env.user_id,
                ticker="600519",
                trade_date="2026-10-17",
                status=status,
                error=error,
                progress_json=analysis_state.dumps(progress or {}),
                result_json=analysis_state.dumps(result) if result else "",
                heartbeat_at=heartbeat,
                control=control,
            )
        )
        session.commit()
    return task_id


def _row(env, task_id: str) -> AnalysisTask:
    with env.session_factory() as session:
        return session.get(AnalysisTask, task_id)


# ---------------------------------------------------------------------------
# 1/2. 跨进程读状态
# ---------------------------------------------------------------------------


def test_snapshot_served_from_db_without_local_tracker(env):
    task_id = _add_task(
        env,
        progress={
            "ticker": "600519",
            "is_running": True,
            "current_stage": "market",
            "stages": [{"id": "market", "status": "active"}],
            "llm_calls": 3,
        },
        heartbeat=datetime.now(timezone.utc),
    )
    assert env.manager.get_tracker(task_id) is None  # 模拟另一个 worker

    snap = env.manager.snapshot(task_id)

    assert snap["state_source"] == "snapshot"
    assert snap["is_running"] is True
    assert snap["current_stage"] == "market"
    assert snap["llm_calls"] == 3
    assert snap["ticker"] == "600519"


def test_snapshot_unknown_task_is_404(env):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        env.manager.snapshot("no-such-task")
    assert exc.value.status_code == 404


def test_row_status_wins_over_stale_progress(env):
    """进度快照说 running，但行已被回收成 stopped → 必须报 stopped."""
    task_id = _add_task(
        env,
        status="stopped",
        progress={"is_running": True, "stop_requested": False},
        error=analysis_state.STALE_MESSAGE,
    )

    snap = env.manager.snapshot(task_id)

    assert snap["is_running"] is False
    assert snap["stop_requested"] is True
    assert snap["error"] == analysis_state.STALE_MESSAGE


# ---------------------------------------------------------------------------
# 3. 跨进程取结果
# ---------------------------------------------------------------------------


def test_result_from_persisted_json(env):
    task_id = _add_task(
        env,
        status="completed",
        result={
            "ticker": "600519",
            "stock_label": "贵州茅台",
            "signal": "Buy",
            "final_state": {"final_trade_decision": "买入"},
        },
    )

    result = env.manager.result(task_id)

    assert result["state_source"] == "snapshot"
    assert result["final_state"]["final_trade_decision"] == "买入"
    assert result["signal"] == "Buy"


def test_result_conflict_when_not_completed(env):
    from fastapi import HTTPException

    task_id = _add_task(env, status="running")
    with pytest.raises(HTTPException) as exc:
        env.manager.result(task_id)
    assert exc.value.status_code == 409


def test_result_conflict_when_legacy_row_has_no_payload(env):
    from fastapi import HTTPException

    task_id = _add_task(env, status="completed")  # 老版本创建的完成行
    with pytest.raises(HTTPException) as exc:
        env.manager.result(task_id)
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# 4. 跨进程控制指令
# ---------------------------------------------------------------------------


def test_remote_pause_writes_control_flag(env):
    task_id = _add_task(env, status="running")

    assert env.manager.pause(task_id) is True
    assert _row(env, task_id).control == analysis_state.CONTROL_PAUSE


def test_remote_pause_rejected_when_not_running(env):
    task_id = _add_task(env, status="stopped")
    assert env.manager.pause(task_id) is False
    assert _row(env, task_id).control == ""


def test_remote_resume_and_stop_allowed_states(env):
    paused = _add_task(env, status="paused")
    assert env.manager.resume(paused) is True
    assert _row(env, paused).control == analysis_state.CONTROL_RESUME

    running = _add_task(env, status="running")
    assert env.manager.stop(running) is True
    assert _row(env, running).control == analysis_state.CONTROL_STOP


# ---------------------------------------------------------------------------
# 5. 执行体消费指令并清空
# ---------------------------------------------------------------------------


def _local_tracker(running: bool = True):
    from web.progress import ProgressTracker

    tracker = ProgressTracker(ticker="600519", trade_date="2026-10-17")
    tracker.is_running = running
    return tracker


def test_local_tracker_consumes_pause_and_clears_flag(env):
    task_id = _add_task(env, status="running", control=analysis_state.CONTROL_PAUSE)
    tracker = _local_tracker()
    env.manager._tasks[task_id] = tracker

    assert env.manager.flush_local() == 1

    assert tracker.is_paused is True
    row = _row(env, task_id)
    assert row.control == ""
    assert row.status == "paused"
    assert analysis_state.loads(row.progress_json)["is_paused"] is True


def test_local_tracker_consumes_stop_and_marks_stopped(env):
    task_id = _add_task(env, status="running", control=analysis_state.CONTROL_STOP)
    tracker = _local_tracker()
    env.manager._tasks[task_id] = tracker

    env.manager.flush_local()

    assert tracker.stop_requested is True
    row = _row(env, task_id)
    assert row.control == ""
    assert row.status == "stopped"


def test_local_flush_writes_progress_and_heartbeat(env):
    task_id = _add_task(env, status="running")
    tracker = _local_tracker()
    tracker.mark_stage_done("market", "技术面走强")
    env.manager._tasks[task_id] = tracker

    env.manager._flush(task_id, tracker, force=True)

    row = _row(env, task_id)
    payload = analysis_state.loads(row.progress_json)
    assert payload["is_running"] is True
    assert payload["stages"][0]["status"] == "done"
    assert payload["stages"][0]["report"] == "技术面走强"
    assert row.heartbeat_at is not None
    assert row.owner == env.manager._owner


def test_flush_skips_rewrite_when_progress_unchanged(env):
    task_id = _add_task(env, status="running")
    tracker = _local_tracker()
    env.manager._tasks[task_id] = tracker
    env.manager._flush(task_id, tracker, force=True)

    first = _row(env, task_id).heartbeat_at
    env.manager._flush(task_id, tracker)          # 内容未变
    second = _row(env, task_id).heartbeat_at
    assert second >= first                        # 只刷心跳, 不重写 JSON


# ---------------------------------------------------------------------------
# 6. 心跳超时才回收（多 worker 关键语义）
# ---------------------------------------------------------------------------


def test_reap_skips_fresh_heartbeat(env):
    fresh = _add_task(env, status="running", heartbeat=datetime.now(timezone.utc))
    stale = _add_task(
        env, status="running",
        heartbeat=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    legacy = _add_task(env, status="paused")       # 老行: heartbeat 为 NULL

    assert env.manager.reap_stale() == 2

    assert _row(env, fresh).status == "running"    # 别的 worker 还活着, 不动
    assert _row(env, stale).status == "stopped"
    assert _row(env, legacy).status == "stopped"
    assert analysis_state.STALE_MESSAGE in _row(env, stale).error
    assert "服务重启" in _row(env, stale).error


def test_sync_task_row_persists_progress(env):
    task_id = _add_task(env, status="running")
    tracker = _local_tracker()
    tracker.mark_stage_done("market", "ok")
    env.manager._tasks[task_id] = tracker

    with env.session_factory() as session:
        row = session.get(AnalysisTask, task_id)
        env.manager.sync_task_row(session, row)
        assert row.status == "running"
        assert "market" in analysis_state.loads(row.progress_json)["stages"][0]["id"]


def test_start_heartbeat_is_idempotent(env):
    manager = env.manager
    manager.start_heartbeat()
    first = manager._hb_thread
    manager.start_heartbeat()
    assert manager._hb_thread is first
    manager._hb_stop.set()


def test_two_managers_share_state(env):
    """显式模拟两个 worker: A 持有执行体, B 只能读表 —— B 必须看得到进度。"""
    task_id = _add_task(env, status="running")
    worker_a = env.manager
    worker_b = analysis_service.AnalysisTaskManager()

    tracker = _local_tracker()
    worker_a._tasks[task_id] = tracker
    tracker.mark_stage_done("market", "A 写下的报告")
    worker_a._flush(task_id, tracker, force=True)

    snap_b = worker_b.snapshot(task_id)
    assert snap_b["state_source"] == "snapshot"
    assert snap_b["stages"][0]["report"] == "A 写下的报告"
    # B 也能发指令, 由 A 消费
    assert worker_b.stop(task_id) is True
    assert worker_a.flush_local() == 1
    assert tracker.stop_requested is True
    assert _row(env, task_id).status == "stopped"


# ---------------------------------------------------------------------------
# 7. 存量库升级（init_db 幂等补列）
# ---------------------------------------------------------------------------


def test_migrate_adds_state_columns_to_legacy_table(tmp_path):
    """老库（无 A5 列）应能直接启动：补列 + 建索引，且可重复执行。"""
    from sqlalchemy import inspect, text

    from app.core import database as database_module

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE analysis_tasks ("
                " id VARCHAR(32) PRIMARY KEY, user_id INTEGER NOT NULL,"
                " ticker VARCHAR(16) NOT NULL, stock_name VARCHAR(64),"
                " trade_date VARCHAR(10) NOT NULL, status VARCHAR(16),"
                " signal VARCHAR(32), error TEXT, llm_provider VARCHAR(48),"
                " quick_think_llm VARCHAR(96), deep_think_llm VARCHAR(96),"
                " lookback_days INTEGER, created_at DATETIME, updated_at DATETIME)"
            )
        )

    database_module._migrate_analysis_task_state(engine)
    database_module._migrate_analysis_task_state(engine)  # 幂等

    columns = {c["name"] for c in inspect(engine).get_columns("analysis_tasks")}
    assert {
        "progress_json", "result_json", "owner",
        "heartbeat_at", "control", "control_at",
    } <= columns
    indexes = {i["name"] for i in inspect(engine).get_indexes("analysis_tasks")}
    assert {"ix_analysis_tasks_owner", "ix_analysis_tasks_heartbeat_at"} <= indexes
