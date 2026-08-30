"""Database CRUD operations for the pipeline.

Provides functions to create/read snapshots and news items (新闻影响力榜).
行业榜/热股榜已迁移到 quant 子系统 (ai_stock.quant.db_ops), 本模块不再
读写 industry_rankings / stock_recommendations。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _get_session():
    """Get a database session.

    Supports both backend context (where app.core.database is available)
    and standalone context (where we create our own engine).
    """
    try:
        from backend.app.core.database import SessionLocal
        return SessionLocal()
    except ImportError:
        pass

    try:
        from app.core.database import SessionLocal
        return SessionLocal()
    except ImportError:
        pass

    # Standalone fallback (quant 选股流程等非后端进程): 复用 quant 统一引擎,
    # 与 backend 指向同一个 backend/data/aistock.db。
    try:
        from sqlalchemy.orm import sessionmaker

        from ai_stock.quant.db import get_engine

        factory = sessionmaker(
            bind=get_engine(), autoflush=False, autocommit=False,
            expire_on_commit=False,
        )
        return factory()
    except Exception as exc:
        logger.warning("No database session available (%s); using null operations", exc)
        return None


def create_snapshot(
    period: str,
    total_news: int = 0,
    status: str = "running",
) -> Optional[int]:
    """Create a new ImpactSnapshot and return its ID."""
    from ai_stock.pipeline.db_models import ImpactSnapshot

    session = _get_session()
    if session is None:
        return None

    try:
        snapshot = ImpactSnapshot(
            snapshot_time=datetime.now(timezone.utc),
            period=period,
            status=status,
            total_news_collected=total_news,
        )
        session.add(snapshot)
        session.commit()
        snapshot_id = snapshot.id
        return snapshot_id
    except Exception as exc:
        logger.error("Failed to create snapshot: %s", exc)
        session.rollback()
        return None
    finally:
        session.close()


def update_snapshot(
    snapshot_id: int,
    status: str | None = None,
    total_news: int | None = None,
    top20_json: str | None = None,
) -> bool:
    """Update an existing snapshot."""
    from ai_stock.pipeline.db_models import ImpactSnapshot

    session = _get_session()
    if session is None:
        return False

    try:
        snapshot = session.get(ImpactSnapshot, snapshot_id)
        if snapshot is None:
            return False
        if status is not None:
            snapshot.status = status
        if total_news is not None:
            snapshot.total_news_collected = total_news
        if top20_json is not None:
            snapshot.top20_json = top20_json
        session.commit()
        return True
    except Exception as exc:
        logger.error("Failed to update snapshot %d: %s", snapshot_id, exc)
        session.rollback()
        return False
    finally:
        session.close()


def save_news_items(snapshot_id: int, news_items: list[dict]) -> int:
    """Save a batch of NewsItem records. Returns count saved."""
    from ai_stock.pipeline.db_models import NewsItem

    session = _get_session()
    if session is None:
        return 0

    count = 0
    try:
        for item in news_items:
            ni = NewsItem(
                snapshot_id=snapshot_id,
                title_hash=item.get("title_hash", ""),
                title=item.get("title", ""),
                content=item.get("content", ""),
                source=item.get("source", ""),
                pub_time=item.get("pub_time", ""),
                category=item.get("category", "news"),
                policy_score=item.get("policy_score", 0.0),
                news_score=item.get("news_score", 0.0),
                capital_score=item.get("capital_score", 0.0),
                sentiment_score=item.get("sentiment_score", 0.0),
                composite_score=item.get("composite_score", 0.0),
                supply_demand_json=item.get("supply_demand_json", ""),
                bull_bear_bias=item.get("bull_bear_bias", "neutral"),
                debate_summary=item.get("debate_summary", ""),
                industries_json=json.dumps(
                    item.get("industries", []), ensure_ascii=False,
                ),
                top_stocks_json=json.dumps(
                    item.get("top_stocks", []), ensure_ascii=False,
                ),
                expected_gain_low=item.get("expected_gain_low", 0.0),
                expected_gain_high=item.get("expected_gain_high", 0.0),
                rank=item.get("rank", 0),
            )
            session.add(ni)
            count += 1
        session.commit()
    except Exception as exc:
        logger.error("Failed to save news items: %s", exc)
        session.rollback()
        count = 0
    finally:
        session.close()
    return count


def get_news_by_industry_ranking(board_id: int) -> Optional[dict]:
    """Get the news items related to an industry-board row (行业榜→新闻).

    行业榜本身由 quant 选股流程产出 (quant_industry_board); 这里用榜行的行业名,
    在最新一份完成的新闻影响力快照中匹配主/副行业 (``industries_json``)
    包含该名称的新闻, 供前端「行业榜→相关新闻」联动展示。
    无快照或无匹配新闻时返回空列表。
    """
    from ai_stock.pipeline.db_models import ImpactSnapshot, NewsItem
    from ai_stock.quant import db_ops as quant_db_ops

    session = _get_session()
    if session is None:
        return None

    try:
        board = quant_db_ops.get_industry_board_row(board_id)
        if board is None:
            return None
        industry = board.get("industry", "")
        if not industry:
            return {"industry": "", "snapshot_id": None, "news_items": []}

        snapshot = (
            session.query(ImpactSnapshot)
            .filter(ImpactSnapshot.status == "completed")
            .order_by(ImpactSnapshot.snapshot_time.desc())
            .first()
        )
        if snapshot is None:
            return {"industry": industry, "snapshot_id": None, "news_items": []}

        # Escape LIKE wildcards inside the industry name (defense in depth;
        # board names are Chinese and normally contain none).
        escaped = (
            industry
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        needle = f'%"{escaped}"%'
        rows = (
            session.query(NewsItem)
            .filter(NewsItem.snapshot_id == snapshot.id)
            .filter(NewsItem.industries_json.like(needle, escape="\\"))
            .order_by(NewsItem.composite_score.desc())
            .all()
        )
        return {
            "industry": industry,
            "snapshot_id": snapshot.id,
            "news_items": [r.to_dict() for r in rows],
        }
    except Exception as exc:
        logger.error("Failed to get news for board %s: %s", board_id, exc)
        return None
    finally:
        session.close()


def get_latest_snapshot() -> Optional[dict]:
    """Get the latest completed snapshot with its data."""
    from ai_stock.pipeline.db_models import ImpactSnapshot, NewsItem

    session = _get_session()
    if session is None:
        return None

    try:
        snapshot = (
            session.query(ImpactSnapshot)
            .filter(ImpactSnapshot.status == "completed")
            .order_by(ImpactSnapshot.snapshot_time.desc())
            .first()
        )
        if snapshot is None:
            return None

        news_items = (
            session.query(NewsItem)
            .filter(NewsItem.snapshot_id == snapshot.id)
            .order_by(NewsItem.rank.asc())
            .all()
        )

        return {
            "snapshot": snapshot.to_dict(),
            "news_items": [n.to_dict() for n in news_items],
        }
    except Exception as exc:
        logger.error("Failed to get latest snapshot: %s", exc)
        return None
    finally:
        session.close()


def get_snapshot_by_date(date_str: str) -> Optional[dict]:
    """Get snapshots for a specific date (YYYY-MM-DD)."""
    from ai_stock.pipeline.db_models import ImpactSnapshot, NewsItem

    session = _get_session()
    if session is None:
        return None

    try:
        snapshots = (
            session.query(ImpactSnapshot)
            .filter(ImpactSnapshot.status == "completed")
            .filter(
                ImpactSnapshot.snapshot_time >= datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            )
            .filter(
                ImpactSnapshot.snapshot_time < datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                + timedelta(days=1)
            )
            .order_by(ImpactSnapshot.snapshot_time.desc())
            .all()
        )

        if not snapshots:
            return None

        # Return the latest one for that date
        snapshot = snapshots[0]
        news_items = (
            session.query(NewsItem)
            .filter(NewsItem.snapshot_id == snapshot.id)
            .order_by(NewsItem.rank.asc())
            .all()
        )

        return {
            "snapshot": snapshot.to_dict(),
            "news_items": [n.to_dict() for n in news_items],
        }
    except Exception as exc:
        logger.error("Failed to get snapshot for %s: %s", date_str, exc)
        return None
    finally:
        session.close()
