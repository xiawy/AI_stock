"""Top 20 impact ranking — final sorting and filtering.

After scoring (Phase 2) and debate (Phase 3), this module produces the
definitive Top 20 ranking by combining composite score and supply-demand
strength, while filtering out bearish items.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .config import TOP_N_IMPACT
from .scoring import ScoredNews
from .supply_demand import SupplyDemandResult

logger = logging.getLogger(__name__)


def rank_top20(
    debated_news: list[dict],
    top_n: int = TOP_N_IMPACT,
) -> list[dict]:
    """Produce the final Top N impact ranking.

    Args:
        debated_news: List of news dicts with all scoring + debate fields populated.
            Each dict must have at minimum:
            - composite_score (float)
            - bull_bear_bias (str: "bullish" / "bearish" / "neutral")
            - supply_demand_json (str, JSON of SupplyDemandResult)
            - all other NewsItem fields
        top_n: Number of items in the final ranking (default 20).

    Returns:
        List of news dicts sorted by rank (1 = most impactful), with
        the ``rank`` field set.
    """
    if not debated_news:
        return []

    # 1. Filter: only keep bullish or neutral items
    candidates = [
        n for n in debated_news
        if n.get("bull_bear_bias", "neutral") in ("bullish", "neutral")
    ]

    if not candidates:
        logger.warning(
            "All %d debated news are bearish; keeping top 5 least-bearish as fallback",
            len(debated_news),
        )
        # Fallback: keep the least bearish ones so the pipeline still produces output
        candidates = sorted(
            debated_news,
            key=lambda n: n.get("composite_score", 0),
            reverse=True,
        )[:5]

    # 2. Sort by composite score (primary) and supply-demand strength (secondary)
    def _sort_key(news: dict) -> tuple[float, float]:
        score = news.get("composite_score", 0.0)
        # Extract supply-demand strength for secondary sort
        sd_strength = _extract_sd_strength(news.get("supply_demand_json", ""))
        return (score, sd_strength)

    candidates.sort(key=_sort_key, reverse=True)

    # 3. Assign ranks
    ranked = candidates[:top_n]
    for i, news in enumerate(ranked, 1):
        news["rank"] = i

    logger.info(
        "Top %d ranking produced from %d candidates "
        "(%d bearish filtered, %d total debated)",
        len(ranked), len(candidates),
        len(debated_news) - len(candidates),
        len(debated_news),
    )

    return ranked


def _extract_sd_strength(supply_demand_json: str) -> float:
    """Extract a numeric strength from supply-demand JSON for secondary sorting.

    Higher = stronger supply-demand gap (more impactful).
    """
    if not supply_demand_json:
        return 0.0
    try:
        data = json.loads(supply_demand_json)
        if not data:
            return 0.0

        # Map gap types to strength values
        gap_type = data.get("gap_type", "balanced")
        gap_strength = {
            "shortage": 3.0,          # 供不应求 — strongest
            "demand_shortage": 2.0,   # 需求增加
            "supply_surplus": 1.5,    # 供给收缩
            "balanced": 0.0,          # 平衡
            "surplus": -1.0,          # 供过于求
        }
        base = gap_strength.get(gap_type, 0.0)

        # Scale by elasticity
        elasticity = data.get("elasticity_coefficient", 0.5)
        return base * elasticity

    except (json.JSONDecodeError, TypeError):
        return 0.0


def build_top20_json(ranked_news: list[dict]) -> str:
    """Build a compact JSON summary of the Top 20 for storage in ImpactSnapshot.

    The full data lives in NewsItem rows; this is a quick-lookup summary.
    """
    summary = []
    for news in ranked_news:
        summary.append({
            "rank": news.get("rank", 0),
            "title": news.get("title", "")[:80],
            "composite_score": round(news.get("composite_score", 0), 2),
            "bull_bear_bias": news.get("bull_bear_bias", "neutral"),
            "industries": news.get("industries", []),
            "expected_gain_low": news.get("expected_gain_low", 0),
            "expected_gain_high": news.get("expected_gain_high", 0),
        })
    return json.dumps(summary, ensure_ascii=False)
