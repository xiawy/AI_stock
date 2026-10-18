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


class NewsAssessCache(Base):
    """LLM 评估结果缓存（按新闻标题 + 提示词版本）。

    选股每 10 分钟一轮、新闻榜 2 小时一次，同一条新闻一天内会被反复评估。
    一次批量评估约 40 条 / 数万 token，命中率实测 >70%，缓存直接把这块成本
    压掉大头。key 里带 ``prompt_version``：改了提示词口径就必须重新评估，
    否则旧口径的结论会一直沿用（这是最容易踩的坑）。
    """

    __tablename__ = "news_assess_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    title_hash: Mapped[str] = mapped_column(String(32), default="", index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    prompt_version: Mapped[str] = mapped_column(String(16), default="")
    payload_json: Mapped[str] = mapped_column(Text, default="")   # NewsAssessment 序列化
    model: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


class NewsImpactTracking(Base):
    """新闻影响力回测台账（预测 → 事后验证）。

    每条落榜新闻的每个主题影响各写一行，``horizon`` 决定验证日：
    1d 下一交易日、3d 三个交易日、1w 五个交易日。``scripts/news_impact_review.py``
    按主题取成分股等权收益回填 ``actual_return``，再判定方向是否命中。

    这张表是"幅度/方向到底靠不靠谱"的唯一客观依据 —— 没有它，所有校准参数
    （``STRENGTH_COEF`` / ``elasticity``）都只是拍脑袋。
    """

    __tablename__ = "news_impact_tracking"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    news_title_hash: Mapped[str] = mapped_column(String(32), default="", index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    theme: Mapped[str] = mapped_column(String(64), default="", index=True)
    direction: Mapped[str] = mapped_column(String(16), default="neutral")
    strength: Mapped[int] = mapped_column(Integer, default=1)
    horizon: Mapped[str] = mapped_column(String(8), default="1d")
    base_date: Mapped[str] = mapped_column(String(10), default="", index=True)  # YYYY-MM-DD
    exp_low: Mapped[float] = mapped_column(Float, default=0.0)
    exp_high: Mapped[float] = mapped_column(Float, default=0.0)
    actual_return: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    direction_hit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    range_hit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


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


def ensure_support_tables(bind=None) -> None:
    """幂等创建缓存表与回测表（DDL 直建，不依赖 Base.metadata）。

    这两张表在 backend 与 quant 两个进程里都可能首次出现，而两边导入的
    ``Base`` 未必是同一个对象（backend 用 app.core.database.Base，quant 用
    QuantBase），走 create_all 有概率漏建。用 ``CREATE TABLE IF NOT EXISTS``
    最稳，且对已有表零影响。
    """
    from sqlalchemy import text

    if bind is None:
        try:
            from app.core.database import engine as bind  # backend 进程
        except ImportError:
            try:
                from backend.app.core.database import engine as bind
            except ImportError:
                from ai_stock.quant.db import get_engine  # quant 独立进程

                bind = get_engine()

    ddl = (
        """CREATE TABLE IF NOT EXISTS news_assess_cache (
            cache_key VARCHAR(64) NOT NULL PRIMARY KEY,
            title_hash VARCHAR(32) DEFAULT '',
            title VARCHAR(512) DEFAULT '',
            prompt_version VARCHAR(16) DEFAULT '',
            payload_json TEXT DEFAULT '',
            model VARCHAR(64) DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS ix_news_assess_created ON news_assess_cache (created_at)",
        """CREATE TABLE IF NOT EXISTS news_impact_tracking (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER DEFAULT 0,
            news_title_hash VARCHAR(32) DEFAULT '',
            title VARCHAR(512) DEFAULT '',
            theme VARCHAR(64) DEFAULT '',
            direction VARCHAR(16) DEFAULT 'neutral',
            strength INTEGER DEFAULT 1,
            horizon VARCHAR(8) DEFAULT '1d',
            base_date VARCHAR(10) DEFAULT '',
            exp_low FLOAT DEFAULT 0,
            exp_high FLOAT DEFAULT 0,
            actual_return FLOAT,
            direction_hit INTEGER,
            range_hit INTEGER,
            verified_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS ix_news_track_theme ON news_impact_tracking (theme)",
        "CREATE INDEX IF NOT EXISTS ix_news_track_base ON news_impact_tracking (base_date)",
        "CREATE INDEX IF NOT EXISTS ix_news_track_snap ON news_impact_tracking (snapshot_id)",
    )
    with bind.begin() as conn:
        for stmt in ddl:
            conn.execute(text(stmt))
