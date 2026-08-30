"""Full pipeline orchestration — news impact assessment end-to-end.

Steps:
1. Collect news → dedup + classify
2. Rule-based pre-filter (keyword) → 50-80 items
3. 4-agent parallel scoring (with primary/secondary industry labels)
4. Supply-demand quantification
5. Composite score >= 6.0 filter
6. Bull/bear debate → Top 20
7. Rank Top 20 → write to DB

行业榜/热股榜不再由本 pipeline 产出: 行业榜 = quant 选股流程生命周期定位的
前 10 行业 (含龙头股), 热股榜 = 自选池活跃标的 (见 ai_stock.quant)。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from .cache import get_cache
from .config import (
    MAX_RETRIES,
    MIN_COMPOSITE_SCORE,
    NEWS_WINDOW_HOURS,
    RETRY_BASE_DELAY,
    TOP_N_IMPACT,
)
from .scoring import ScoredNews, score_all_news
from .supply_demand import quantify_supply_demand
from .debate import debate_batch
from .ranking import rank_top20, build_top20_json
from . import db_ops
from . import evolution as pipeline_evolution

logger = logging.getLogger(__name__)


def run_full_pipeline(
    config: dict,
    llm_quick: Any,
    llm_deep: Any,
    curr_date: str | None = None,
) -> dict:
    """Run the complete impact assessment + stock recommendation pipeline.

    Args:
        config: Global config dict (from DEFAULT_CONFIG).
        llm_quick: Quick-thinking LLM instance.
        llm_deep: Deep-thinking LLM instance.
        curr_date: Override date (default: today).

    Returns:
        Dict with pipeline results:
        - snapshot_id: int
        - top20: list of news dicts
        - status: "completed" / "failed"
    """
    if curr_date is None:
        curr_date = datetime.now().strftime("%Y-%m-%d")

    # Determine period (AM/PM)
    hour = datetime.now().hour
    period = "AM" if hour < 12 else "PM"

    logger.info("=" * 60)
    logger.info("Pipeline started: %s %s (date=%s)", period, curr_date, curr_date)
    logger.info("=" * 60)

    # Initialize evolution system (self-memory + custom strategies)
    evo_ctx = pipeline_evolution.init_evolution(config)
    if evo_ctx:
        logger.info("Evolution enabled: %d chars context loaded", len(evo_ctx))
    else:
        logger.info("Evolution disabled or no context available")

    cache = get_cache()
    cache.clear_expired()

    # Create snapshot
    snapshot_id = db_ops.create_snapshot(period=period, status="running")

    try:
        # ── Step 1: Collect news ──
        logger.info("[1/7] Collecting news...")
        news_items = _retry_call(
            _collect_news, curr_date, NEWS_WINDOW_HOURS,
        )
        logger.info("Collected %d news items", len(news_items))

        if not news_items:
            logger.warning("No news collected; using fallback")
            return _fallback_pipeline(snapshot_id, period)

        # ── Step 2: Pre-filter ──
        logger.info("[2/7] Pre-filtering...")
        filtered = _prefilter_news(news_items)
        logger.info("After pre-filter: %d items", len(filtered))

        # ── Step 3: 4-agent parallel scoring ──
        logger.info("[3/7] 4-agent scoring...")
        scored = score_all_news(filtered, llm_quick, MIN_COMPOSITE_SCORE)
        logger.info("Scored: %d items pass threshold", len(scored))

        # ── Step 4: Filter by composite score (already done inside score_all_news,
        #   but kept explicit for clarity / future threshold changes) ──
        logger.info("[4/7] Filtering by composite score...")
        candidates = [sn for sn in scored if sn.composite >= MIN_COMPOSITE_SCORE]
        logger.info("Candidates after filter: %d", len(candidates))

        # ── Step 5: Supply-demand quantification (only for candidates that pass) ──
        logger.info("[5/7] Supply-demand analysis...")
        for sn in candidates:
            try:
                sd = quantify_supply_demand(
                    sn.news, sn.supply_demand_signals, llm_quick,
                )
                sn._supply_demand_result = sd
            except Exception as exc:
                logger.warning("Supply-demand failed for '%s': %s", sn.news.get("title", "")[:30], exc)

        # ── Step 6: Debate → Top 20 ──
        logger.info("[6/7] News debate...")
        debate_input = [
            (sn.news, _scored_to_dict(sn))
            for sn in candidates
        ]
        verdicts = debate_batch(debate_input, llm_quick, llm_deep)

        # Merge verdict + supply-demand results into the SAME dicts (no re-conversion)
        debated_news = []
        for (_, news_dict), verdict in zip(debate_input, verdicts):
            news_dict["bull_bear_bias"] = verdict.bull_bear_bias
            news_dict["debate_summary"] = verdict.debate_summary
            # supply-demand data is attached to the ScoredNews via _supply_demand_result;
            # we retrieve it from the candidate list by index
            debated_news.append(news_dict)

        # Attach supply-demand JSON + expected gains from candidates
        for sn, news_dict in zip(candidates, debated_news):
            sd = getattr(sn, "_supply_demand_result", None)
            if sd:
                news_dict["supply_demand_json"] = sd.model_dump_json()
                news_dict["expected_gain_low"] = sd.expected_gain_low
                news_dict["expected_gain_high"] = sd.expected_gain_high
            else:
                news_dict["supply_demand_json"] = ""
                news_dict["expected_gain_low"] = 0.0
                news_dict["expected_gain_high"] = 0.0

        # ── Step 7: Rank Top 20 ──
        logger.info("[7/7] Ranking Top %d...", TOP_N_IMPACT)
        top20 = rank_top20(debated_news, TOP_N_IMPACT)
        top20_json = build_top20_json(top20)

        # Save news items to DB
        if snapshot_id:
            db_ops.save_news_items(snapshot_id, top20)
            db_ops.update_snapshot(
                snapshot_id,
                total_news=len(news_items),
                top20_json=top20_json,
                status="completed",
            )

        logger.info("=" * 60)
        logger.info(
            "Pipeline completed: Top %d news (industry/hot-stock boards are "
            "produced by the quant selection flow)",
            len(top20),
        )
        logger.info("=" * 60)

        return {
            "snapshot_id": snapshot_id,
            "top20": top20,
            "status": "completed",
        }

    except Exception as exc:
        logger.error("Pipeline failed: %s", exc, exc_info=True)
        if snapshot_id:
            db_ops.update_snapshot(snapshot_id, status="failed")
        return {
            "snapshot_id": snapshot_id,
            "top20": [],
            "status": "failed",
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _collect_news(curr_date: str, hours: int) -> list[dict]:
    """Collect news via the data layer."""
    from ai_stock.dataflows.pipeline_data import get_impact_news
    return get_impact_news(curr_date, hours)


def _prefilter_news(news_items: list[dict]) -> list[dict]:
    """Rule-based pre-filter: remove obviously irrelevant news.

    Keeps items that have:
    - Policy keywords OR
    - Market-relevant keywords OR
    - Are classified as 'policy'
    """
    _RELEVANT_KEYWORDS = frozenset({
        "股", "A股", "涨", "跌", "板", "涨停", "跌停", "利好", "利空",
        "政策", "央行", "证监会", "国务院", "降息", "降准", "资金",
        "行业", "产业", "芯片", "半导体", "新能源", "锂电", "光伏",
        "医药", "消费", "地产", "银行", "保险", "科技", "AI", "人工",
        "机器人", "汽车", "军工", "航天", "数据", "算力", "通信",
        "稀土", "钢铁", "煤炭", "有色", "化工", "农业", "食品",
        "出口", "进口", "贸易", "关税", "制裁", "改革", "创新",
        "IPO", "融资", "分红", "回购", "增持", "减持",
    })

    filtered = []
    for item in news_items:
        title = item.get("title", "")
        content = item.get("content", "")[:200]
        text = title + " " + content

        # Always keep policy-classified items
        if item.get("category") == "policy":
            filtered.append(item)
            continue

        # Check for relevant keywords
        hits = sum(1 for kw in _RELEVANT_KEYWORDS if kw in text)
        if hits >= 1:
            filtered.append(item)

    logger.info("Pre-filter: %d/%d items kept", len(filtered), len(news_items))
    return filtered


def _scored_to_dict(sn: ScoredNews) -> dict:
    """Convert a ScoredNews to a dict suitable for debate + ranking."""
    d = sn.to_dict()
    # Add supply demand signals for debate
    d["supply_demand_signals"] = sn.supply_demand_signals
    return d


def _retry_call(fn, *args, max_retries: int = MAX_RETRIES):
    """Call fn with exponential backoff retry."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            return fn(*args)
        except Exception as exc:
            last_exc = exc
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(
                "Retry %d/%d for %s: %s (waiting %.1fs)",
                attempt + 1, max_retries, fn.__name__, exc, delay,
            )
            time.sleep(delay)
    raise last_exc


def _fallback_pipeline(snapshot_id, period: str) -> dict:
    """Fallback: reuse the previous snapshot's Top-20 news list."""
    logger.warning("Using fallback: loading previous snapshot")
    prev = db_ops.get_latest_snapshot()
    if prev:
        if snapshot_id:
            db_ops.update_snapshot(snapshot_id, status="completed")
        return {
            "snapshot_id": snapshot_id,
            "top20": prev.get("news_items", []),
            "status": "completed",
            "fallback": True,
        }

    # No previous data either
    if snapshot_id:
        db_ops.update_snapshot(snapshot_id, status="failed")
    return {
        "snapshot_id": snapshot_id,
        "top20": [],
        "status": "failed",
        "error": "No news collected and no previous snapshot available",
    }
