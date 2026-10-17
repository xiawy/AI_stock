"""Analysis task ORM model — per-user record of pipeline runs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class AnalysisTask(Base):
    __tablename__ = "analysis_tasks"

    # UUID hex issued by the task manager; stable across process restarts
    # (matching tracker objects live only in memory).
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    ticker: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    stock_name: Mapped[str] = mapped_column(String(64), default="")
    trade_date: Mapped[str] = mapped_column(String(10), index=True, nullable=False)

    # running | paused | completed | error | stopped
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    signal: Mapped[str] = mapped_column(String(32), default="")
    error: Mapped[str] = mapped_column(Text, default="")

    # LLM configuration snapshot for reproducibility
    llm_provider: Mapped[str] = mapped_column(String(48), default="")
    quick_think_llm: Mapped[str] = mapped_column(String(96), default="")
    deep_think_llm: Mapped[str] = mapped_column(String(96), default="")
    lookback_days: Mapped[int] = mapped_column(Integer, nullable=True)

    # ── A5: 任务状态表内化 (任意 worker 都能服务任意任务) ──────────────
    # progress_json: 与 /status 返回值同构的进度快照 (阶段/报告/计数/耗时)
    # result_json:   完成时的最终结果 (final_state + stock_label), 供非执行进程返回
    # owner:         持有执行线程的进程标识 host:pid
    # heartbeat_at:  执行体心跳; 超时视为进程已退出 (据此回收, 而非启动即杀)
    # control:       跨进程控制指令 pause|resume|stop, 由持有执行体的进程消费后清空
    progress_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(96), default="", index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    control: Mapped[str] = mapped_column(String(16), default="")
    control_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user: Mapped["User"] = relationship()  # noqa: F821

    def progress(self) -> dict[str, Any]:
        """已持久化的进度快照 (解析失败返回空字典, 不抛异常)。"""
        try:
            value = json.loads(self.progress_json or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def to_dict(self) -> dict:
        return {
            "task_id": self.id,
            "user_id": self.user_id,
            "ticker": self.ticker,
            "stock_name": self.stock_name,
            "trade_date": self.trade_date,
            "status": self.status,
            "signal": self.signal,
            "error": self.error,
            "llm_provider": self.llm_provider,
            "quick_think_llm": self.quick_think_llm,
            "deep_think_llm": self.deep_think_llm,
            "lookback_days": self.lookback_days,
            "owner": self.owner,
            "heartbeat_at": (
                self.heartbeat_at.isoformat() if self.heartbeat_at else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
