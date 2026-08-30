"""新闻榜生成 — 取选股流程采集的热点新闻前 20 条落库.

新闻榜不再走独立的重型评估流程 (多 Agent 评分/辩论已移除), 而是直接
复用 quant 选股流程第一步 (MacroEventAgent) 抓取的近 3 日全市场热点
新闻/政策快讯:

- 排序口径: 政策类 (category == "policy") 优先, 同类内按发布时间倒序
- 落库时机: 选股流程抓完新闻后即写入 ImpactSnapshot + NewsItem,
  与原快照表/读取 API/备份完全兼容
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


def select_top_news(
    news_items: list[dict],
    top_n: int,
) -> list[dict]:
    """从原始新闻列表中选出前 *top_n* 条并打上 rank.

    排序: 政策类优先 (policy > 其它), 同类内按 ``time`` 字符串倒序
    (格式统一为 "YYYY-MM-DD HH:MM", 字典序即时间序). 无标题的条目剔除.
    返回的是原始 dict 的浅拷贝列表, 不修改入参.
    """
    valid = [
        dict(n) for n in (news_items or [])
        if str(n.get("title", "")).strip()
    ]
    valid.sort(
        key=lambda n: (
            1 if n.get("category") == "policy" else 0,
            str(n.get("time", "")),
        ),
        reverse=True,
    )
    top = valid[:top_n]
    for i, item in enumerate(top, 1):
        item["rank"] = i
    return top


def save_news_board(
    news_items: list[dict],
    top_n: Optional[int] = None,
) -> dict:
    """选出前 *top_n* 条新闻并作为一份完成的快照落库.

    返回 ``{"snapshot_id": ..., "saved": ...}``; 新闻为空或落库失败时
    ``snapshot_id`` 为 None. 本函数不抛异常, 调用方 (选股流程) 不受影响.
    """
    from . import db_ops
    from .config import TOP_N_IMPACT

    if top_n is None:
        top_n = TOP_N_IMPACT

    top = select_top_news(news_items, top_n)
    if not top:
        logger.info("News board: no news items to save")
        return {"snapshot_id": None, "saved": 0}

    try:
        period = "AM" if datetime.now().hour < 12 else "PM"
        snapshot_id = db_ops.create_snapshot(
            period=period,
            total_news=len(news_items or []),
            status="running",
        )
        if snapshot_id is None:
            logger.warning("News board: snapshot creation failed")
            return {"snapshot_id": None, "saved": 0}
        saved = db_ops.save_news_items(snapshot_id, [_to_news_row(n) for n in top])
        db_ops.update_snapshot(snapshot_id, status="completed")
        logger.info(
            "News board saved: snapshot=%s, %d/%d items",
            snapshot_id, saved, len(news_items or []),
        )
        return {"snapshot_id": snapshot_id, "saved": saved}
    except Exception as exc:
        logger.warning("News board save failed: %s", exc)
        return {"snapshot_id": None, "saved": 0}


def _to_news_row(item: dict) -> dict:
    """原始新闻条目 → NewsItem 落库行 (``time`` → ``pub_time`` 映射)."""
    return {
        "title_hash": item.get("title_hash", ""),
        "title": str(item.get("title", ""))[:512],
        "content": item.get("content", ""),
        "source": item.get("source", ""),
        "pub_time": item.get("time", "") or item.get("pub_time", ""),
        "category": item.get("category", "news"),
        "bull_bear_bias": "neutral",
        "rank": item.get("rank", 0),
    }
