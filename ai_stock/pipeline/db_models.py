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
# sys.path configurations. 独立进程 (如 quant 选股流程落库新闻榜) 两者都
# 不可导入时退回 quant 子系统的 Base — 双侧指向同一个统一 SQLite 文件。
try:
    from app.core.database import Base
except ImportError:
    try:
        from backend.app.core.database import Base
    except ImportError:
        from ai_stock.quant.db import QuantBase as Base


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

    # ── 事件维度（2026-10 新闻榜重构新增，见 docs/NEWS_IMPACT_REDESIGN.md）──
    # event_type 接替 category 的"二分"职责：policy/industry/company/capital/overseas
    event_type: Mapped[str] = mapped_column(String(16), default="")
    importance: Mapped[float] = mapped_column(Float, default=0.0)      # 0~10，8+ = 必看
    bias_strength: Mapped[int] = mapped_column(Integer, default=0)     # 多空强度 1~3
    horizon: Mapped[str] = mapped_column(String(8), default="")        # 1d / 3d / 1w
    confidence: Mapped[float] = mapped_column(Float, default=0.0)      # 0~1
    industry_impacts_json: Mapped[str] = mapped_column(Text, default="")  # [{name,direction,...}]
    cluster_id: Mapped[str] = mapped_column(String(16), default="")    # 同事件多源合并
    source_count: Mapped[int] = mapped_column(Integer, default=1)      # 几家源报道

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
            # 事件维度（新）。event_type 缺省时回落 category，保证旧快照前端不空。
            "event_type": self.event_type or self.category or "news",
            "importance": self.importance or 0.0,
            "bias_strength": self.bias_strength or 0,
            "horizon": self.horizon or "",
            "confidence": self.confidence or 0.0,
            "industry_impacts": (
                json.loads(self.industry_impacts_json)
                if self.industry_impacts_json else []
            ),
            "cluster_id": self.cluster_id or "",
            "source_count": self.source_count or 1,
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


# ── 幂等补列 ────────────────────────────────────────────────────────────
# create_all 不会给**已存在**的表补列，而 news_items 在现网库里已经跑了一年多。
# 这里的列清单必须与 NewsItem 的映射保持一致，两个进程（backend / quant worker）
# 都会调用：backend 在 init_db() 里，quant 在首次落新闻榜前。
NEWS_IMPACT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("event_type", "VARCHAR(16) DEFAULT ''"),
    ("importance", "FLOAT DEFAULT 0"),
    ("bias_strength", "INTEGER DEFAULT 0"),
    ("horizon", "VARCHAR(8) DEFAULT ''"),
    ("confidence", "FLOAT DEFAULT 0"),
    ("industry_impacts_json", "TEXT DEFAULT ''"),
    ("cluster_id", "VARCHAR(16) DEFAULT ''"),
    ("source_count", "INTEGER DEFAULT 1"),
)

_schema_ready = False


def ensure_news_impact_schema(bind=None) -> None:
    """给已存在的 ``news_items`` 表幂等补上事件维度列。

    只跑一次（进程级缓存）。表不存在时直接返回 —— create_all 会按完整定义建表。
    """
    global _schema_ready
    if _schema_ready:
        return
    from sqlalchemy import inspect, text

    if bind is None:
        try:
            from app.core.database import engine as bind  # backend 进程
        except ImportError:
            try:
                from backend.app.core.database import engine as bind
            except ImportError:
                from ai_stock.quant.db import get_engine  # quant 独立进程

                bind = get_engine()

    inspector = inspect(bind)
    if "news_items" not in inspector.get_table_names():
        _schema_ready = True
        return
    existing = {col["name"] for col in inspector.get_columns("news_items")}
    missing = [
        (name, ddl) for name, ddl in NEWS_IMPACT_COLUMNS if name not in existing
    ]
    if missing:
        with bind.begin() as conn:
            for name, ddl in missing:
                conn.execute(text(f"ALTER TABLE news_items ADD COLUMN {name} {ddl}"))
    _schema_ready = True

