"""Database CRUD operations for the pipeline.

Provides functions to create/read snapshots, news items, and recommendations.
Handles the fallback logic: if the current pipeline run fails, the previous
snapshot is used for stock recommendation.
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

    # Standalone fallback: create an in-memory session
    logger.warning("No database session available; using null operations")
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


def save_recommendations(snapshot_id: int, recommendations: list[dict]) -> int:
    """Save a batch of StockRecommendation records. Returns count saved."""
    from ai_stock.pipeline.db_models import StockRecommendation

    session = _get_session()
    if session is None:
        return 0

    count = 0
    try:
        for rec in recommendations:
            sr = StockRecommendation(
                snapshot_id=snapshot_id,
                ticker=rec.get("ticker", ""),
                stock_name=rec.get("stock_name", ""),
                industry=rec.get("industry", ""),
                trigger_event=rec.get("trigger_event", ""),
                buy_logic=rec.get("buy_logic", ""),
                fundamentals_score=rec.get("fundamentals_score", 0.0),
                technical_score=rec.get("technical_score", 0.0),
                event_match_score=rec.get("event_match_score", 0.0),
                debate_score=rec.get("debate_score", 0.0),
                final_score=rec.get("final_score", 0.0),
                target_price=rec.get("target_price", 0.0),
                expected_gain_low=rec.get("expected_gain_low", 0.0),
                expected_gain_high=rec.get("expected_gain_high", 0.0),
                stop_loss_price=rec.get("stop_loss_price", 0.0),
                holding_period=rec.get("holding_period", ""),
                risk_level=rec.get("risk_level", ""),
                bull_bear_summary=rec.get("bull_bear_summary", ""),
                rank=rec.get("rank", 0),
                is_alternate=rec.get("is_alternate", False),
            )
            session.add(sr)
            count += 1
        session.commit()
    except Exception as exc:
        logger.error("Failed to save recommendations: %s", exc)
        session.rollback()
        count = 0
    finally:
        session.close()
    return count


def save_industry_rankings(snapshot_id: int, rankings: list[dict]) -> int:
    """Save a batch of IndustryRanking records. Returns count saved."""
    from ai_stock.pipeline.db_models import IndustryRanking

    session = _get_session()
    if session is None:
        return 0

    count = 0
    try:
        for item in rankings:
            ir = IndustryRanking(
                snapshot_id=snapshot_id,
                industry=item.get("industry", ""),
                industry_code=item.get("industry_code", ""),
                heat_score=item.get("heat_score", 0.0),
                news_count=item.get("news_count", 0),
                fund_flow_net=item.get("fund_flow_net"),
                change_pct=item.get("change_pct"),
                resonance=item.get("resonance", "none"),
                rating=item.get("rating", "C"),
                leader_stocks_json=json.dumps(
                    item.get("leader_stocks", []), ensure_ascii=False,
                ),
                rank=item.get("rank", 0),
            )
            session.add(ir)
            count += 1
        session.commit()
    except Exception as exc:
        logger.error("Failed to save industry rankings: %s", exc)
        session.rollback()
        count = 0
    finally:
        session.close()
    return count


def _load_industry_rows(snapshot_id: int, session) -> list:
    """Load IndustryRanking rows for a snapshot, ordered by rank."""
    from ai_stock.pipeline.db_models import IndustryRanking

    return (
        session.query(IndustryRanking)
        .filter(IndustryRanking.snapshot_id == snapshot_id)
        .order_by(IndustryRanking.rank.asc())
        .all()
    )


def get_latest_industry_rankings() -> Optional[dict]:
    """Get the latest completed snapshot with its industry rankings."""
    from ai_stock.pipeline.db_models import ImpactSnapshot

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
        rows = _load_industry_rows(snapshot.id, session)
        return {
            "snapshot": snapshot.to_dict(),
            "rankings": [r.to_dict() for r in rows],
        }
    except Exception as exc:
        logger.error("Failed to get latest industry rankings: %s", exc)
        return None
    finally:
        session.close()


def get_industry_rankings_by_date(date_str: str) -> Optional[dict]:
    """Get the latest industry rankings snapshot for a date (YYYY-MM-DD)."""
    from ai_stock.pipeline.db_models import ImpactSnapshot

    session = _get_session()
    if session is None:
        return None

    try:
        snapshots = (
            session.query(ImpactSnapshot)
            .filter(ImpactSnapshot.status == "completed")
            .filter(
                ImpactSnapshot.snapshot_time
                >= datetime.strptime(date_str, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            )
            .filter(
                ImpactSnapshot.snapshot_time
                < datetime.strptime(date_str, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
                + timedelta(days=1)
            )
            .order_by(ImpactSnapshot.snapshot_time.desc())
            .all()
        )
        if not snapshots:
            return None
        snapshot = snapshots[0]
        rows = _load_industry_rows(snapshot.id, session)
        return {
            "snapshot": snapshot.to_dict(),
            "rankings": [r.to_dict() for r in rows],
        }
    except Exception as exc:
        logger.error("Failed to get industry rankings for %s: %s", date_str, exc)
        return None
    finally:
        session.close()


def get_news_by_industry_ranking(ranking_id: int) -> Optional[dict]:
    """Get the news items behind an industry-ranking row (行业榜→新闻).

    Loads the IndustryRanking row, then returns the same snapshot's news
    items whose primary/secondary industry list (``industries_json``)
    contains the ranking's industry name — i.e. exactly the news that fed
    the row's heat score. Ordered by composite score, best first.
    """
    from ai_stock.pipeline.db_models import IndustryRanking, NewsItem

    session = _get_session()
    if session is None:
        return None

    try:
        ranking = session.get(IndustryRanking, ranking_id)
        if ranking is None:
            return None

        # Escape LIKE wildcards inside the industry name (defense in depth;
        # board names are Chinese and normally contain none).
        escaped = (
            ranking.industry
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        needle = f'%"{escaped}"%'
        rows = (
            session.query(NewsItem)
            .filter(NewsItem.snapshot_id == ranking.snapshot_id)
            .filter(NewsItem.industries_json.like(needle, escape="\\"))
            .order_by(NewsItem.composite_score.desc())
            .all()
        )
        return {
            "industry": ranking.industry,
            "snapshot_id": ranking.snapshot_id,
            "news_items": [r.to_dict() for r in rows],
        }
    except Exception as exc:
        logger.error("Failed to get news for ranking %s: %s", ranking_id, exc)
        return None
    finally:
        session.close()


def get_latest_snapshot() -> Optional[dict]:
    """Get the latest completed snapshot with its data."""
    from ai_stock.pipeline.db_models import ImpactSnapshot, NewsItem, StockRecommendation

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

        recommendations = (
            session.query(StockRecommendation)
            .filter(StockRecommendation.snapshot_id == snapshot.id)
            .order_by(StockRecommendation.rank.asc())
            .all()
        )

        return {
            "snapshot": snapshot.to_dict(),
            "news_items": [n.to_dict() for n in news_items],
            "recommendations": [r.to_dict() for r in recommendations],
        }
    except Exception as exc:
        logger.error("Failed to get latest snapshot: %s", exc)
        return None
    finally:
        session.close()


def snapshot_exists_for_date(date_str: str) -> bool:
    """Return True if a completed snapshot exists for the given date (YYYY-MM-DD)."""
    from ai_stock.pipeline.db_models import ImpactSnapshot

    session = _get_session()
    if session is None:
        return False

    try:
        day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        row = (
            session.query(ImpactSnapshot.id)
            .filter(ImpactSnapshot.status == "completed")
            .filter(ImpactSnapshot.snapshot_time >= day_start)
            .filter(ImpactSnapshot.snapshot_time < day_start + timedelta(days=1))
            .first()
        )
        return row is not None
    except Exception as exc:
        logger.error("Failed to check snapshot for %s: %s", date_str, exc)
        return False
    finally:
        session.close()


def get_snapshot_by_date(date_str: str) -> Optional[dict]:
    """Get snapshots for a specific date (YYYY-MM-DD)."""
    from ai_stock.pipeline.db_models import ImpactSnapshot, NewsItem, StockRecommendation

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
        recommendations = (
            session.query(StockRecommendation)
            .filter(StockRecommendation.snapshot_id == snapshot.id)
            .order_by(StockRecommendation.rank.asc())
            .all()
        )

        return {
            "snapshot": snapshot.to_dict(),
            "news_items": [n.to_dict() for n in news_items],
            "recommendations": [r.to_dict() for r in recommendations],
        }
    except Exception as exc:
        logger.error("Failed to get snapshot for %s: %s", date_str, exc)
        return None
    finally:
        session.close()
