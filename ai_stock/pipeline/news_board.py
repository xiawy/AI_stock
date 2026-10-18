"""新闻榜生成 — 选股流程采集的热点新闻 → 富化 → 配额排序 → 落库.

新闻榜不再走独立的重型评估流程（多 Agent 评分/辩论），而是复用 quant 选股
流程第一步（MacroEventAgent）抓取的全市场热点新闻/政策快讯，再经
``ai_stock.pipeline.news_impact`` 富化：

```
归一化去重 → 规则初筛 + 事件聚类 → 批量 LLM 评估（可选）
  → 主题对齐（只认 active 的 L3 主题）→ 幅度校准 → 配额排序 → 快照节流落库
```

三条与原实现的差别（原实现的问题见 ``docs/NEWS_IMPACT_REDESIGN.md``）：

1. 排序不再是"政策优先 + 时间倒序"——那是"只有政策类"的直接原因（677 条里
   政策 ≥20 就必然 100% 政策）。改为配额：政策 ≤8、行业 ≥6、公司 ≥3、
   资金 ≥2、海外 ≥1，某类不足时名额让给其它类。
2. ``_to_news_row`` 补齐全部字段。原实现只写 6 个字段，其余落 DB 默认值，
   前端四张评分卡/行业/多空/涨幅全部显示 0 或"—"。
3. 快照节流：2 小时内且榜单内容差异 <30% 时复用上一份快照，不再每次
   ``macro_event`` 都新建一份（线上曾一天写 12~19 份）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# 新浪 rich_text 等常把真标题包在开头【】里、后接正文; 取【】内主标题做去重键。
_LEAD_BRACKET_RE = re.compile(r"^【(.+?)】")
# 去重键剔除所有非「字母/数字/汉字/下划线」字符 (空白 + 中英文标点)。
_NON_WORD_RE = re.compile(r"\W+", re.UNICODE)

# 快照节流：距上一份快照 < 2h 且榜单内容差异 < 30% 时复用
THROTTLE_HOURS = 2.0
THROTTLE_DIFF = 0.30


def normalize_title(title: str) -> str:
    """标题规范化去重键: 提取开头【】内主标题 → 去空白与标点 → 小写。

    用于识别「同一新闻多源转发」的重复: 各源标题常仅在标点、空格、
    【来源】前缀上有差异 (如东财「…竞争秩序 加强…」与同花顺「…竞争秩序，加强…」)。
    """
    text = (title or "").strip()
    m = _LEAD_BRACKET_RE.match(text)
    if m:
        text = m.group(1).strip()
    return _NON_WORD_RE.sub("", text).lower()


def dedup_news(items: list[dict], title_key: str = "title") -> list[dict]:
    """按规范化标题去重, 保留首次出现者 (调用方负责先按优先级排序)。

    规范化键为空的条目 (无有效标题) 原样保留、不参与去重, 避免误删。
    只对「标题规范化后完全相等」去重 (标点/空格/【】前缀差异);
    措辞改写的相似新闻不合并, 以免误删不同新闻。
    """
    seen: set[str] = set()
    out: list[dict] = []
    for item in items or []:
        key = normalize_title(str(item.get(title_key, "")))
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(item)
    return out


def select_top_news(
    news_items: list[dict],
    top_n: int,
) -> list[dict]:
    """从原始新闻列表中选出前 *top_n* 条并打上 rank（**配额排序**）。

    未富化的条目会先走 ``news_impact.prepare`` 的规则层拿到 event_type /
    importance，因此即便 LLM 不可用，榜单也保证类型多样、行业和多空有值。
    返回的是浅拷贝，不修改入参。
    """
    from . import news_impact

    prepared = news_impact.prepare(news_items, limit=max(top_n * 4, 80))
    if not prepared:
        return []
    enriched = news_impact.enrich(prepared, llm=None, llm_limit=0)
    return news_impact.rank_with_quota(enriched, top_n)


def save_news_board(
    news_items: list[dict],
    top_n: Optional[int] = None,
    llm=None,
    force: bool = False,
) -> dict:
    """选出前 *top_n* 条新闻，富化后作为一份完成的快照落库。

    返回 ``{"snapshot_id": ..., "saved": ..., "skipped": ...}``。新闻为空或落库
    失败时 ``snapshot_id`` 为 None。本函数不抛异常，调用方（选股流程）不受影响。

    *llm* 为 quant 的 ``QuantLLM`` 包装（``llm.pick()`` 取底层客户端）；为 None
    时走纯规则降级。*force* 绕过快照节流（手工触发用）。
    """
    from . import db_ops, news_impact
    from .config import TOP_N_IMPACT
    from .db_models import ensure_news_impact_schema

    if top_n is None:
        top_n = TOP_N_IMPACT

    ensure_news_impact_schema()

    prepared = news_impact.prepare(news_items)
    if not prepared:
        logger.info("News board: no news items to save")
        return {"snapshot_id": None, "saved": 0, "skipped": "empty"}

    # 节流：先算指纹（纯本地，不花 token），命中则整段跳过 LLM 与落库
    fp = news_impact.fingerprint(prepared, top_n=max(top_n * 2, 40))
    if not force:
        reused = _throttled_snapshot(fp)
        if reused is not None:
            logger.info("News board: reused snapshot %s (content unchanged)", reused)
            db_ops.update_snapshot(reused, total_news=len(news_items or []))
            return {"snapshot_id": reused, "saved": 0, "skipped": "throttled"}

    client = None
    if llm is not None:
        try:
            client = llm.pick() if hasattr(llm, "pick") else llm
        except Exception as exc:
            logger.warning("News board: llm unavailable (%s); rule mode", exc)

    try:
        enriched = news_impact.enrich(prepared, llm=client)
        top = news_impact.rank_with_quota(enriched, top_n)
        if not top:
            return {"snapshot_id": None, "saved": 0, "skipped": "empty"}

        period = "AM" if datetime.now().hour < 12 else "PM"
        snapshot_id = db_ops.create_snapshot(
            period=period,
            total_news=len(news_items or []),
            status="running",
        )
        if snapshot_id is None:
            logger.warning("News board: snapshot creation failed")
            return {"snapshot_id": None, "saved": 0, "skipped": "db_error"}
        saved = db_ops.save_news_items(snapshot_id, [_to_news_row(n) for n in top])
        db_ops.update_snapshot(
            snapshot_id, status="completed",
            top20_json=json.dumps(
                [{"title": n["title"], "event_type": n.get("event_type", "")}
                 for n in top],
                ensure_ascii=False,
            ),
        )
        logger.info(
            "News board saved: snapshot=%s, %d/%d items, types=%s",
            snapshot_id, saved, len(news_items or []),
            _type_histogram(top),
        )
        return {"snapshot_id": snapshot_id, "saved": saved, "skipped": ""}
    except Exception as exc:
        logger.warning("News board save failed: %s", exc)
        return {"snapshot_id": None, "saved": 0, "skipped": "error"}


def _throttled_snapshot(fp: set[str]) -> Optional[int]:
    """最近快照与当前指纹差异 < ``THROTTLE_DIFF`` 且足够新 → 返回其 id。"""
    from . import db_ops, news_impact

    try:
        recent = db_ops.get_recent_snapshot_titles(hours=THROTTLE_HOURS)
    except Exception as exc:
        logger.debug("throttle check failed: %s", exc)
        return None
    if not recent:
        return None
    if news_impact.content_diff(fp, set(recent.get("titles") or [])) <= THROTTLE_DIFF:
        return recent.get("snapshot_id")
    return None


def _type_histogram(items: list[dict]) -> dict:
    hist: dict[str, int] = {}
    for item in items:
        key = item.get("event_type") or "unknown"
        hist[key] = hist.get(key, 0) + 1
    return hist


def _to_news_row(item: dict) -> dict:
    """富化后的新闻 → NewsItem 落库行。

    旧的四维分（policy/news/capital/sentiment_score）**只在本类维度上等于
    importance，其余为 0** —— 我们并没有做四次独立评分，填 0 是对齐真相；
    前端已改为展示 importance / confidence / bias_strength / source_count。
    `composite_score` 保留为 importance 的别名，旧排序逻辑与外部消费者不受影响。
    """
    impacts = item.get("industry_impacts") or []
    importance = float(item.get("importance") or 0.0)
    etype = str(item.get("event_type") or "")
    primary = impacts[0] if impacts else {}

    def _dim(kind: str) -> float:
        return round(importance, 2) if etype == kind else 0.0

    return {
        "title_hash": item.get("title_hash", ""),
        "title": str(item.get("title", ""))[:512],
        "content": item.get("content", ""),
        "source": item.get("source", ""),
        "pub_time": item.get("time", "") or item.get("pub_time", ""),
        "category": "policy" if etype == "policy" else item.get("category", "news"),
        # 事件维度（新）
        "event_type": etype,
        "importance": importance,
        "bias_strength": int(item.get("bias_strength") or item.get("strength") or 0),
        "horizon": item.get("horizon", ""),
        "confidence": float(item.get("confidence") or 0.0),
        "industry_impacts_json": json.dumps(impacts, ensure_ascii=False),
        "cluster_id": item.get("cluster_id", ""),
        "source_count": int(item.get("source_count") or 1),
        # 兼容旧列
        "policy_score": _dim("policy"),
        "news_score": _dim("industry"),
        "capital_score": _dim("capital"),
        "sentiment_score": round(float(item.get("confidence") or 0.0) * 10, 2),
        "composite_score": importance,
        "bull_bear_bias": item.get("bull_bear_bias", "neutral"),
        "debate_summary": str(item.get("reasoning", ""))[:400],
        "industries": item.get("industries", []),
        "top_stocks": item.get("top_stocks", []),
        "expected_gain_low": float(primary.get("exp_low") or 0.0),
        "expected_gain_high": float(primary.get("exp_high") or 0.0),
        "rank": item.get("rank", 0),
    }
