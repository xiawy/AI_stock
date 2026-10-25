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
                event_type=item.get("event_type", ""),
                importance=item.get("importance", 0.0),
                bias_strength=item.get("bias_strength", 0),
                horizon=item.get("horizon", ""),
                confidence=item.get("confidence", 0.0),
                industry_impacts_json=item.get("industry_impacts_json", ""),
                cluster_id=item.get("cluster_id", ""),
                source_count=item.get("source_count", 1),
                bias_scope=item.get("bias_scope", ""),
                assess_source=item.get("assess_source", ""),
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

    行业榜由 quant 选股流程产出 (quant_industry_board), 行业名为东财细分
    板块名 (如「半导体材料」「锂电池」)。

    **归因口径统一 (2026-10)**: 优先用新闻榜自己的 ``industry_impacts``
    (主题级 direction, 与新闻榜"影响行业"列同源), 匹配不上才退回
    ``event_industry_kb.match_industries`` 从正文现算。此前只有现算一条路径,
    同一条新闻在新闻榜与行业榜弹窗里可能归到不同行业, 两个榜一起失信。
    """
    from ai_stock.pipeline.db_models import ImpactSnapshot, NewsItem
    from ai_stock.pipeline.news_board import dedup_news
    from ai_stock.quant import db_ops as quant_db_ops
    from ai_stock.quant.event_industry_kb import (
        industry_matches_any,
        match_industries,
    )

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

        rows = (
            session.query(NewsItem)
            .filter(NewsItem.snapshot_id == snapshot.id)
            .order_by(NewsItem.rank.asc())
            .all()
        )
        news_items: list[dict] = []
        for r in rows:
            item = r.to_dict()
            match = _match_board_industry(r, industry, match_industries,
                                          industry_matches_any)
            if match is None:
                continue
            tags, matched_by = match
            if not item.get("industries"):
                item["industries"] = tags
            item["matched_by"] = matched_by
            news_items.append(item)
        return {
            "industry": industry,
            "snapshot_id": snapshot.id,
            "news_items": dedup_news(news_items),
        }
    except Exception as exc:
        logger.error("Failed to get news for board %s: %s", board_id, exc)
        return None
    finally:
        session.close()


def _impact_theme_names(industry_impacts_json: str) -> list[str]:
    """``industry_impacts_json`` → 主题名列表 (与新闻榜"影响行业"列同源)."""
    try:
        parsed = json.loads(industry_impacts_json) if industry_impacts_json else []
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    for entry in parsed:
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            if name:
                out.append(name)
    return out


def _match_board_industry(row, industry: str, kb_matcher, any_matcher):
    """判断一条新闻是否属于行业榜某行, 命中则返回 (标签, 命中路径)。

    优先级 = 可信度优先级, 不是性能优先级:
    1. ``industry_impacts`` —— 榜单自己评估出的主题, 与新闻榜同源;
    2. ``industries_json`` —— 旧版落库标签;
    3. 知识库现算 —— 兜底, 同一事实可能归到另一个行业, 所以排在最后;
    4. 行业名直接出现在正文 —— 最弱的文本兜底。
    """
    text = f"{row.title or ''} {(row.content or '')[:500]}"

    impact_tags = _impact_theme_names(row.industry_impacts_json)
    if impact_tags and any_matcher(industry, impact_tags):
        return impact_tags, "impact"

    stored: list[str] = []
    if row.industries_json:
        try:
            parsed = json.loads(row.industries_json)
            if isinstance(parsed, list):
                stored = [str(x) for x in parsed if x]
        except (ValueError, TypeError):
            stored = []
    if stored and any_matcher(industry, stored):
        return stored, "stored"

    kb_tags = kb_matcher(text) or []
    if kb_tags and any_matcher(industry, kb_tags):
        return kb_tags, "kb"

    if industry in text:
        return impact_tags or stored or kb_tags or [industry], "text"
    return None


def get_recent_snapshot_titles(hours: float = 2.0) -> Optional[dict]:
    """最近 *hours* 小时内最后一份 completed 快照的 id 与标题集合（节流用）。

    无快照或超出时间窗返回 None —— 调用方据此决定是否写新快照。
    """
    from ai_stock.pipeline.db_models import ImpactSnapshot, NewsItem

    session = _get_session()
    if session is None:
        return None
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        snapshot = (
            session.query(ImpactSnapshot)
            .filter(ImpactSnapshot.status == "completed")
            .order_by(ImpactSnapshot.snapshot_time.desc())
            .first()
        )
        if snapshot is None:
            return None
        snap_time = snapshot.snapshot_time
        if snap_time is not None:
            if snap_time.tzinfo is None:
                snap_time = snap_time.replace(tzinfo=timezone.utc)
            if snap_time < cutoff:
                return None
        titles = [
            r.title for r in session.query(NewsItem)
            .filter(NewsItem.snapshot_id == snapshot.id).all()
        ]
        return {"snapshot_id": snapshot.id, "titles": titles}
    except Exception as exc:
        logger.warning("get_recent_snapshot_titles failed: %s", exc)
        return None
    finally:
        session.close()


def get_latest_snapshot() -> Optional[dict]:
    """Get the latest completed snapshot with its data."""
    from ai_stock.pipeline.db_models import ImpactSnapshot, NewsItem
    from ai_stock.pipeline.news_board import dedup_news

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
            "news_items": dedup_news([n.to_dict() for n in news_items]),
        }
    except Exception as exc:
        logger.error("Failed to get latest snapshot: %s", exc)
        return None
    finally:
        session.close()


def get_snapshot_by_date(date_str: str) -> Optional[dict]:
    """Get snapshots for a specific date (YYYY-MM-DD)."""
    from ai_stock.pipeline.db_models import ImpactSnapshot, NewsItem
    from ai_stock.pipeline.news_board import dedup_news

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
            "news_items": dedup_news([n.to_dict() for n in news_items]),
        }
    except Exception as exc:
        logger.error("Failed to get snapshot for %s: %s", date_str, exc)
        return None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 同事件多源成员 (news_cluster_member)
# ---------------------------------------------------------------------------


def save_cluster_members(snapshot_id: int, enriched: list[dict],
                         picked: list[dict]) -> int:
    """保存同事件多源成员, 一行一条转载。

    *enriched* 是富化后的全量候选(带 cluster_id), *picked* 是上榜条目 ——
    上榜那条标 ``is_representative=1``。只为"上榜过的簇"写成员: 没上榜的簇
    写了也没人展开, 纯占库。失败静默(成员明细是锦上添花, 不能拖垮榜单落库)。
    """
    from ai_stock.pipeline.db_models import NewsClusterMember

    if not snapshot_id or not enriched:
        return 0
    cluster_ids = {str(x.get("cluster_id") or "") for x in picked} - {""}
    if not cluster_ids:
        return 0
    rep_keys = {
        (str(x.get("cluster_id") or ""), str(x.get("title") or ""))
        for x in picked
    }

    session = _get_session()
    if session is None:
        return 0
    try:
        count = 0
        for item in enriched:
            cid = str(item.get("cluster_id") or "")
            if cid not in cluster_ids:
                continue
            title = str(item.get("title") or "")
            session.add(NewsClusterMember(
                snapshot_id=snapshot_id,
                cluster_id=cid,
                title=title,
                source=str(item.get("source") or ""),
                pub_time=str(item.get("pub_time") or ""),
                is_representative=1 if (cid, title) in rep_keys else 0,
            ))
            count += 1
        session.commit()
        return count
    except Exception as exc:
        logger.warning("save_cluster_members failed: %s", exc)
        session.rollback()
        return 0
    finally:
        session.close()


def get_cluster_members(snapshot_id: int, cluster_id: str) -> list[dict]:
    """某簇的多源成员。表不存在(未跑过新版落库)时返回空, 不报错。"""
    from ai_stock.pipeline.db_models import NewsClusterMember

    session = _get_session()
    if session is None:
        return []
    try:
        rows = (
            session.query(NewsClusterMember)
            .filter(NewsClusterMember.snapshot_id == snapshot_id)
            .filter(NewsClusterMember.cluster_id == cluster_id)
            .all()
        )
        return [r.to_dict() for r in rows]
    except Exception as exc:
        logger.debug("get_cluster_members skipped: %s", exc)
        return []
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 跨期对比与成绩单 (只读)
# ---------------------------------------------------------------------------


def get_previous_snapshot() -> Optional[dict]:
    """上一份 completed 快照(相对于最新一份)的 id 与元信息。

    新闻榜"变化"要靠相邻两份快照对比: 一天有 3 份, 所以"上一份"不是"昨天",
    而是时间上紧邻的那一份。
    """
    from ai_stock.pipeline.db_models import ImpactSnapshot

    session = _get_session()
    if session is None:
        return None
    try:
        rows = (
            session.query(ImpactSnapshot)
            .filter(ImpactSnapshot.status == "completed")
            .order_by(ImpactSnapshot.snapshot_time.desc())
            .limit(2)
            .all()
        )
        if len(rows) < 2:
            return None
        prev = rows[1]
        return {
            "id": prev.id,
            "snapshot_time": (
                prev.snapshot_time.isoformat() if prev.snapshot_time else None
            ),
            "period": prev.period,
        }
    except Exception as exc:
        logger.warning("get_previous_snapshot failed: %s", exc)
        return None
    finally:
        session.close()


def get_ranking_map(snapshot_id: int) -> dict[str, dict]:
    """快照内的实体 → {rank, title}, 供跨期 diff 使用。

    键优先用 ``cluster_id``: 同一事件换一家媒体转载时标题会变, 但簇不变。
    带 title 是为了"掉榜"那一栏能显示人话而不是一串哈希。
    """
    from ai_stock.pipeline.db_models import NewsItem
    from ai_stock.pipeline.news_board import normalize_title

    session = _get_session()
    if session is None:
        return {}
    try:
        rows = (
            session.query(NewsItem)
            .filter(NewsItem.snapshot_id == snapshot_id)
            .order_by(NewsItem.rank.asc())
            .all()
        )
        out: dict[str, dict] = {}
        for r in rows:
            key = r.cluster_id or normalize_title(r.title or "")
            if key and key not in out:
                out[key] = {"rank": int(r.rank or 0), "title": r.title or ""}
        return out
    except Exception as exc:
        logger.warning("get_ranking_map failed: %s", exc)
        return {}
    finally:
        session.close()


def get_news_tracking_stats(days: int = 30) -> dict:
    """新闻榜成绩单: 近 *days* 天方向/区间命中率, 按 event_type 与主题分组。

    分母只算 ``direction_hit`` 非空的已判定样本 —— 未到验证日的不计入, 否则
    "还没涨"会被算成"判断错"。
    """
    from ai_stock.pipeline.db_models import NewsImpactTracking, NewsItem

    session = _get_session()
    if session is None:
        return {"ok": False, "reason": "数据库不可用"}
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = (
            session.query(NewsImpactTracking)
            .filter(NewsImpactTracking.base_date >= cutoff)
            .all()
        )
        snap_ids = {r.snapshot_id for r in rows if r.snapshot_id}
        # 主题 → 榜单条目的事件类型(同快照内按主题名反查)
        type_of: dict[tuple[int, str], str] = {}
        if snap_ids:
            items = (
                session.query(NewsItem)
                .filter(NewsItem.snapshot_id.in_(list(snap_ids)))
                .all()
            )
            for it in items:
                for name in _impact_theme_names(it.industry_impacts_json):
                    type_of[(it.snapshot_id, name)] = it.event_type or "news"

        judged = [r for r in rows if r.direction_hit is not None]
        pending = len(rows) - len(judged)

        def _agg(group: list) -> dict:
            n = len(group)
            if not n:
                return {"n": 0, "direction_hit_rate": None, "range_hit_rate": None}
            d = sum(1 for r in group if r.direction_hit == 1)
            rng = [r for r in group if r.range_hit is not None]
            return {
                "n": n,
                "direction_hit_rate": round(d / n, 3),
                "range_hit_rate": (
                    round(sum(1 for r in rng if r.range_hit == 1) / len(rng), 3)
                    if rng else None
                ),
            }

        by_type: dict[str, list] = {}
        by_theme: dict[str, list] = {}
        for r in judged:
            t = type_of.get((r.snapshot_id, r.theme), "unknown")
            by_type.setdefault(t, []).append(r)
            by_theme.setdefault(r.theme or "未标主题", []).append(r)

        return {
            "ok": True,
            "window_days": days,
            "total": len(rows),
            "judged": len(judged),
            "pending": pending,
            "overall": _agg(judged),
            "by_event_type": {k: _agg(v) for k, v in by_type.items()},
            "by_theme": {
                k: _agg(v) for k, v in
                sorted(by_theme.items(), key=lambda kv: -len(kv[1]))[:12]
            },
        }
    except Exception as exc:
        logger.error("get_news_tracking_stats failed: %s", exc)
        return {"ok": False, "reason": str(exc)}
    finally:
        session.close()
