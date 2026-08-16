"""Top 20 impact ranking — final sorting and filtering.

After scoring (Phase 2) and debate (Phase 3), this module produces the
definitive Top 20 ranking by combining composite score and supply-demand
strength, while filtering out bearish items. It also aggregates news
heat by industry into the industry heatmap (行业榜).
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
            "primary_industry": news.get("primary_industry", ""),
            "secondary_industry": news.get("secondary_industry", ""),
            "industries": news.get("industries", []),
            "expected_gain_low": news.get("expected_gain_low", 0),
            "expected_gain_high": news.get("expected_gain_high", 0),
        })
    return json.dumps(summary, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Industry heatmap (行业榜) — news-driven industry heat aggregation
# ---------------------------------------------------------------------------

# Weight of a news item's contribution via its primary vs secondary industry.
PRIMARY_INDUSTRY_WEIGHT = 1.0
SECONDARY_INDUSTRY_WEIGHT = 0.5

# Bull/bear bias discount on industry heat: bearish news about an industry
# still signals attention but should not push it up the bullish ranking.
_BIAS_FACTOR = {"bullish": 1.0, "neutral": 0.6, "bearish": 0.2}

# Heat-score fraction above which an industry counts as "hot" for the
# resonance/rating logic (relative to the hottest industry of this run).
_HOT_HEAT_FRACTION = 0.5

# Fund-flow (relative to the largest absolute net inflow) above which money
# counts as "meaningfully flowing in".
_MEANINGFUL_INFLOW_FRACTION = 0.10


def calculate_industry_heatmap(
    debated_news: list[dict],
    industry_flows: list[dict] | None = None,
    top_n: int = 10,
) -> list[dict]:
    """Aggregate debated news into an industry heat ranking (行业榜).

    Algorithm (加权热度聚合):
        heat(industry) = Σ_news composite_score × industry_weight × bias_factor
        - primary_industry contributes weight 1.0, secondary 0.5
        - bullish news counts fully, neutral 0.6, bearish 0.2

    Each aggregated industry is then matched against the realtime industry
    fund-flow board data (Eastmoney). When "舆论热度" (news heat) and
    "主力资金净流入" (main capital net inflow) align, the industry gets a
    higher resonance/rating; hot news + outflow = divergence warning.

    Pure function — no HTTP calls. Leader stocks are attached later by the
    pipeline (see ``pipeline.py`` Step 8).

    Args:
        debated_news: Ranked/debated news dicts with composite_score,
            bull_bear_bias, primary_industry, secondary_industry.
        industry_flows: Output of ``get_industry_fund_flow()`` (optional;
            when None/empty the ranking degrades to news heat only).
        top_n: Number of industries to return (default 10).

    Returns:
        List of industry dicts sorted by heat_score desc, each with:
        industry, industry_code, heat_score, news_count, fund_flow_net
        (元, None when unmatched), change_pct, resonance, rating, top_stock_*.
    """
    heat: dict[str, dict] = {}

    for news in debated_news:
        score = news.get("composite_score", 0.0)
        if score <= 0:
            continue
        bias = _BIAS_FACTOR.get(news.get("bull_bear_bias", "neutral"), 0.6)
        for industry, weight in (
            (news.get("primary_industry", ""), PRIMARY_INDUSTRY_WEIGHT),
            (news.get("secondary_industry", ""), SECONDARY_INDUSTRY_WEIGHT),
        ):
            industry = (industry or "").strip()
            if not industry:
                continue
            entry = heat.setdefault(
                industry,
                {"industry": industry, "heat_score": 0.0, "news_count": 0},
            )
            entry["heat_score"] += score * weight * bias
            entry["news_count"] += 1

    if not heat:
        return []

    # Fund-flow matching: exact name first, then bidirectional substring
    # (LLM outputs 申万-style names like “电子”, boards are named like
    # “半导体” / “电子元件” — substring matching bridges the two taxonomies).
    flows_by_name: dict[str, dict] = {
        f.get("name", ""): f for f in (industry_flows or []) if f.get("name")
    }

    def _match_flow(industry: str) -> dict | None:
        if not flows_by_name:
            return None
        if industry in flows_by_name:
            return flows_by_name[industry]
        for name, flow in flows_by_name.items():
            if industry in name or name in industry:
                return flow
        return None

    max_abs_inflow = max(
        (abs(f.get("main_net_inflow", 0.0)) for f in (industry_flows or [])),
        default=0.0,
    )

    max_heat = max(e["heat_score"] for e in heat.values()) or 1.0

    ranked: list[dict] = []
    for entry in heat.values():
        flow = _match_flow(entry["industry"])
        fund_flow_net = flow.get("main_net_inflow") if flow else None
        inflow_positive = (
            fund_flow_net is not None
            and fund_flow_net > max_abs_inflow * _MEANINGFUL_INFLOW_FRACTION
        )
        inflow_negative = fund_flow_net is not None and fund_flow_net < 0
        is_hot = entry["heat_score"] >= max_heat * _HOT_HEAT_FRACTION
        # Resonance classification (舆论热度 × 资金流)
        if fund_flow_net is None:
            resonance = "none"
        elif is_hot and inflow_positive:
            resonance = "strong"        # 热度资金共振
        elif is_hot and inflow_negative:
            resonance = "divergence"    # 舆论热但资金流出 — 谨慎
        elif not is_hot and inflow_positive:
            resonance = "quiet"         # 资金潜伏，热度未起
        else:
            resonance = "none"

        # Rating: A = heat + capital resonance; B = one strong signal;
        # C = fallback.
        heat_norm = entry["heat_score"] / max_heat
        if is_hot and inflow_positive:
            rating = "A"
        elif is_hot or (heat_norm >= 0.3 and fund_flow_net is not None and fund_flow_net > 0):
            rating = "B"
        else:
            rating = "C"

        ranked.append({
            "industry": entry["industry"],
            "industry_code": flow.get("code", "") if flow else "",
            "heat_score": round(entry["heat_score"], 2),
            "news_count": entry["news_count"],
            "fund_flow_net": fund_flow_net,
            "change_pct": flow.get("change_pct") if flow else None,
            "resonance": resonance,
            "rating": rating,
            "top_stock_name": flow.get("top_stock_name", "") if flow else "",
            "top_stock_code": flow.get("top_stock_code", "") if flow else "",
        })

    ranked.sort(key=lambda r: r["heat_score"], reverse=True)
    for i, row in enumerate(ranked[:top_n], 1):
        row["rank"] = i
    return ranked[:top_n]
