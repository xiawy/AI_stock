"""SQLAlchemy ORM models for the impact assessment pipeline.

Two tables:
- ``impact_snapshots`` — one row per 12h pipeline run
- ``news_items`` — individual news/policy evaluation results (Top 20)

行业榜 / 热股榜已迁移到 quant 子系统 (quant_industry_board / 自选池),
本模块只保留新闻影响力榜。

These models share the same SQLAlchemy engine as the rest of the backend
(``backend/app/core/database.py``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

# Lazy import to avoid circular dependency — the Base is defined in the
# backend's database module.  Support both project-root and backend-root
# sys.path configurations.
try:
    from app.core.database import Base
except ImportError:
    from backend.app.core.database import Base


class ImpactSnapshot(Base):
    """One complete pipeline run (every 12h)."""

    __tablename__ = "impact_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    period: Mapped[str] = mapped_column(String(4), nullable=False)  # "AM" / "PM"
    status: Mapped[str] = mapped_column(
        String(16), default="running", nullable=False
    )  # running | completed | failed
    total_news_collected: Mapped[int] = mapped_column(Integer, default=0)
    top20_json: Mapped[str] = mapped_column(Text, default="")  # Top 20 summary JSON

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    news_items: Mapped[list["NewsItem"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "snapshot_time": self.snapshot_time.isoformat() if self.snapshot_time else None,
            "period": self.period,
            "status": self.status,
            "total_news_collected": self.total_news_collected,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class NewsItem(Base):
    """Individual news/policy evaluation result."""

    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("impact_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title_hash: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(64), default="")
    pub_time: Mapped[str] = mapped_column(String(32), default="")
    category: Mapped[str] = mapped_column(String(16), default="news")  # policy | news

    # Agent scores (1-10)
    policy_score: Mapped[float] = mapped_column(Float, default=0.0)
    news_score: Mapped[float] = mapped_column(Float, default=0.0)
    capital_score: Mapped[float] = mapped_column(Float, default=0.0)
    sentiment_score: Mapped[float] = mapped_column(Float, default=0.0)
    composite_score: Mapped[float] = mapped_column(Float, default=0.0)

    # Supply-demand analysis
    supply_demand_json: Mapped[str] = mapped_column(Text, default="")

    # Debate outcome
    bull_bear_bias: Mapped[str] = mapped_column(
        String(16), default="neutral"
    )  # bullish | bearish | neutral
    debate_summary: Mapped[str] = mapped_column(Text, default="")

    # Impact analysis
    industries_json: Mapped[str] = mapped_column(Text, default="")  # JSON array
    top_stocks_json: Mapped[str] = mapped_column(Text, default="")  # JSON [{code, name, elasticity}]
    expected_gain_low: Mapped[float] = mapped_column(Float, default=0.0)
    expected_gain_high: Mapped[float] = mapped_column(Float, default=0.0)

    # Final rank (1-20 for Top 20, 0 = not ranked)
    rank: Mapped[int] = mapped_column(Integer, default=0)

    # Relationship
    snapshot: Mapped["ImpactSnapshot"] = relationship(back_populates="news_items")

    def to_dict(self) -> dict:
        import json

        return {
            "id": self.id,
            "snapshot_id": self.snapshot_id,
            "title": self.title,
            # Full news body — the impact detail dialog shows it verbatim.
            "content": self.content,
            "source": self.source,
            "pub_time": self.pub_time,
            "category": self.category,
            "policy_score": self.policy_score,
            "news_score": self.news_score,
            "capital_score": self.capital_score,
            "sentiment_score": self.sentiment_score,
            "composite_score": self.composite_score,
            "supply_demand": json.loads(self.supply_demand_json) if self.supply_demand_json else {},
            "bull_bear_bias": self.bull_bear_bias,
            "debate_summary": self.debate_summary,
            "industries": json.loads(self.industries_json) if self.industries_json else [],
            "top_stocks": json.loads(self.top_stocks_json) if self.top_stocks_json else [],
            "expected_gain_low": self.expected_gain_low,
            "expected_gain_high": self.expected_gain_high,
            "rank": self.rank,
        }
