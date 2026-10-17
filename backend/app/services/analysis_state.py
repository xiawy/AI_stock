"""A5: 分析任务状态持久化 —— 任务表即状态源。

背景：``AnalysisTaskManager`` 原先只把 ``ProgressTracker`` 放在进程内存，
``uvicorn --workers 1`` 因此是硬约束 —— A worker 起的任务在 B worker 里
一律 404。中期方案把「状态」与「执行体」解耦：

- **状态**（进度 / 阶段报告 / 最终结果 / 控制指令）全部落 ``analysis_tasks`` 表；
- **执行体** 仍是发起进程里的线程，但按固定节奏写 ``heartbeat_at``；
- 心跳超时 → 视为执行进程已退出，才回收该行（**不再启动即杀所有 running**，
  否则多 worker 下新 worker 一启动就会杀掉别的 worker 上活着的任务）；
- 控制指令（pause / resume / stop）写 ``control`` 列，由**持有执行体**的进程
  轮询落地并清空。

由此：任意 worker 都能读状态、取结果、发指令；重启后的孤儿行由心跳兜底回收。

本模块保持"纯函数 + 显式传入 Session"，不导入 FastAPI，便于单测。
"""

from __future__ import annotations

import json
import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.analysis_task import AnalysisTask

logger = logging.getLogger(__name__)

# 心跳节奏与失效阈值。阈值取 24 倍心跳间隔：LLM 长等待是 I/O 阻塞，
# 心跳线程照常运行；给足余量是为了避免一次长时间 GC / 换页被误判为死亡。
HEARTBEAT_INTERVAL_SECONDS = 5
STALE_AFTER_SECONDS = 120

# 可下发的控制指令
CONTROL_PAUSE = "pause"
CONTROL_RESUME = "resume"
CONTROL_STOP = "stop"
CONTROL_VALUES = (CONTROL_PAUSE, CONTROL_RESUME, CONTROL_STOP)

STALE_MESSAGE = (
    "服务重启或执行进程已退出（心跳超时），任务已中断；"
    "可从历史记录重新发起分析"
)


def owner_id() -> str:
    """当前进程标识 ``host:pid``（多 worker 下唯一，跨主机也唯一）。"""
    try:
        host = socket.gethostname() or "unknown"
    except OSError:  # pragma: no cover - 极端环境下取不到主机名
        host = "unknown"
    return f"{host}:{os.getpid()}"


def dumps(payload: Any) -> str:
    """序列化为 JSON 文本；不可序列化的值降级为字符串（不让持久化打断业务）。"""
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 防御性
        logger.warning("task payload not serializable: %s", exc)
        return "{}"


def loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return {} if default is None else default
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {} if default is None else default
    return value if isinstance(value, (dict, list)) else ({} if default is None else default)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def persist_progress(
    db: Session,
    task_id: str,
    payload: dict[str, Any],
    *,
    status: str,
    owner: str,
    error: str | None = None,
    signal: str | None = None,
    result: dict[str, Any] | None = None,
) -> bool:
    """写入一次进度快照 + 心跳。返回是否命中行。"""
    values: dict[str, Any] = {
        "progress_json": dumps(payload),
        "status": status,
        "owner": owner,
        "heartbeat_at": now_utc(),
        "updated_at": now_utc(),
    }
    if error is not None:
        values["error"] = error
    if signal is not None:
        values["signal"] = signal
    if result is not None:
        values["result_json"] = dumps(result)
    result_proxy = db.execute(
        update(AnalysisTask).where(AnalysisTask.id == task_id).values(**values)
    )
    db.commit()
    return bool(result_proxy.rowcount)


def touch_heartbeat(db: Session, task_ids: list[str], owner: str) -> None:
    """只刷新心跳（进度无变化时用，避免反复重写大 JSON）。"""
    if not task_ids:
        return
    db.execute(
        update(AnalysisTask)
        .where(AnalysisTask.id.in_(task_ids))
        .values(heartbeat_at=now_utc(), updated_at=now_utc(), owner=owner)
    )
    db.commit()


def load_controls(db: Session, task_ids: list[str]) -> list[tuple[str, str]]:
    """读取待消费的控制指令 ``[(task_id, control), ...]``。"""
    if not task_ids:
        return []
    rows = db.execute(
        select(AnalysisTask.id, AnalysisTask.control).where(
            AnalysisTask.id.in_(task_ids), AnalysisTask.control != ""
        )
    ).all()
    return [(row[0], row[1]) for row in rows]


def clear_control(db: Session, task_id: str, expected: str) -> None:
    """消费后清空指令。``expected`` 用于避免误清另一条新指令。"""
    db.execute(
        update(AnalysisTask)
        .where(AnalysisTask.id == task_id, AnalysisTask.control == expected)
        .values(control="", control_at=now_utc(), updated_at=now_utc())
    )
    db.commit()


def set_control(db: Session, task_id: str, control: str) -> bool:
    """写入跨进程控制指令（调用方负责校验当前状态是否允许）。"""
    result_proxy = db.execute(
        update(AnalysisTask)
        .where(AnalysisTask.id == task_id)
        .values(control=control, control_at=now_utc(), updated_at=now_utc())
    )
    db.commit()
    return bool(result_proxy.rowcount)


def reap_stale_tasks(
    db: Session,
    *,
    stale_after: int = STALE_AFTER_SECONDS,
    message: str = STALE_MESSAGE,
) -> int:
    """把心跳超时的 running/paused 行标为 stopped。

    心跳新鲜（其它 worker 正在跑）的行不动 —— 这是多 worker 与单 worker 的
    关键差别：旧实现是启动时把所有 running 一律冻结。
    """
    cutoff = now_utc() - timedelta(seconds=stale_after)
    result_proxy = db.execute(
        update(AnalysisTask)
        .where(
            AnalysisTask.status.in_(("running", "paused")),
            (AnalysisTask.heartbeat_at.is_(None))
            | (AnalysisTask.heartbeat_at < cutoff),
        )
        .values(
            status="stopped",
            error=message,
            control="",
            updated_at=now_utc(),
        )
    )
    db.commit()
    return int(result_proxy.rowcount or 0)
