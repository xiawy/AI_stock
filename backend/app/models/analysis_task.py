"""Analysis task ORM model — per-user record of pipeline runs."""

from __future__ import annotations

from datetime import datetime, timezone

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

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user: Mapped["User"] = relationship()  # noqa: F821

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
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
