"""SQLAlchemy ORM models for the impact assessment & recommendation pipeline.

Four tables:
- ``impact_snapshots`` — one row per 12h pipeline run
- ``news_items`` — individual news/policy evaluation results (Top 20)
- ``industry_rankings`` — news-heat aggregated industry board ranking (行业榜)
- ``stock_recommendations`` — final stock picks (Top 10 + 3 alternates)

These models share the same SQLAlchemy engine as the rest of the backend
(``backend/app/core/database.py``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
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
    industry_rankings: Mapped[list["IndustryRanking"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )
    recommendations: Mapped[list["StockRecommendation"]] = relationship(
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


class IndustryRanking(Base):
    """News-heat aggregated industry board entry (行业榜).

    One row per hot industry per snapshot. ``heat_score`` aggregates the
    composite scores of all debated news whose primary/secondary industry
    maps here; ``fund_flow_net`` is the realtime main-capital net inflow
    (主力净流入, 元) when the industry matched an Eastmoney board.
    """

    __tablename__ = "industry_rankings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("impact_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    industry: Mapped[str] = mapped_column(String(64), nullable=False)
    industry_code: Mapped[str] = mapped_column(String(16), default="")  # BKxxxx

    heat_score: Mapped[float] = mapped_column(Float, default=0.0)
    news_count: Mapped[int] = mapped_column(Integer, default=0)

    # Realtime board data (nullable when the industry matched no board)
    fund_flow_net: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )  # 主力净流入, 元
    change_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # strong | divergence | quiet | none (舆论热度 × 资金流共振)
    resonance: Mapped[str] = mapped_column(String(16), default="none")
    rating: Mapped[str] = mapped_column(String(2), default="C")  # A | B | C

    # JSON array [{code, name, change_pct, market_cap}] — leaders by market cap
    leader_stocks_json: Mapped[str] = mapped_column(Text, default="")

    rank: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationship
    snapshot: Mapped["ImpactSnapshot"] = relationship(
        back_populates="industry_rankings"
    )

    def to_dict(self) -> dict:
        import json

        return {
            "id": self.id,
            "snapshot_id": self.snapshot_id,
            "industry": self.industry,
            "industry_code": self.industry_code,
            "heat_score": self.heat_score,
            "news_count": self.news_count,
            "fund_flow_net": self.fund_flow_net,
            "change_pct": self.change_pct,
            "resonance": self.resonance,
            "rating": self.rating,
            "leader_stocks": json.loads(self.leader_stocks_json)
            if self.leader_stocks_json
            else [],
            "rank": self.rank,
        }


class StockRecommendation(Base):
    """Final stock recommendation (Top 10 + 3 alternates)."""

    __tablename__ = "stock_recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("impact_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    stock_name: Mapped[str] = mapped_column(String(64), default="")
    industry: Mapped[str] = mapped_column(String(64), default="")
    trigger_event: Mapped[str] = mapped_column(Text, default="")
    buy_logic: Mapped[str] = mapped_column(Text, default="")

    # Scores
    fundamentals_score: Mapped[float] = mapped_column(Float, default=0.0)
    technical_score: Mapped[float] = mapped_column(Float, default=0.0)
    event_match_score: Mapped[float] = mapped_column(Float, default=0.0)
    debate_score: Mapped[float] = mapped_column(Float, default=0.0)
    final_score: Mapped[float] = mapped_column(Float, default=0.0)

    # Price targets
    target_price: Mapped[float] = mapped_column(Float, default=0.0)
    expected_gain_low: Mapped[float] = mapped_column(Float, default=0.0)
    expected_gain_high: Mapped[float] = mapped_column(Float, default=0.0)
    stop_loss_price: Mapped[float] = mapped_column(Float, default=0.0)

    # Holding & risk
    holding_period: Mapped[str] = mapped_column(String(16), default="")  # 短线 | 中线
    risk_level: Mapped[str] = mapped_column(String(8), default="")  # 高 | 中 | 低
    bull_bear_summary: Mapped[str] = mapped_column(Text, default="")

    # Ranking
    rank: Mapped[int] = mapped_column(Integer, default=0)
    is_alternate: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationship
    snapshot: Mapped["ImpactSnapshot"] = relationship(back_populates="recommendations")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "snapshot_id": self.snapshot_id,
            "ticker": self.ticker,
            "stock_name": self.stock_name,
            "industry": self.industry,
            "trigger_event": self.trigger_event,
            "buy_logic": self.buy_logic,
            "fundamentals_score": self.fundamentals_score,
            "technical_score": self.technical_score,
            "event_match_score": self.event_match_score,
            "debate_score": self.debate_score,
            "final_score": self.final_score,
            "target_price": self.target_price,
            "expected_gain_low": self.expected_gain_low,
            "expected_gain_high": self.expected_gain_high,
            "stop_loss_price": self.stop_loss_price,
            "holding_period": self.holding_period,
            "risk_level": self.risk_level,
            "bull_bear_summary": self.bull_bear_summary,
            "rank": self.rank,
            "is_alternate": self.is_alternate,
        }
