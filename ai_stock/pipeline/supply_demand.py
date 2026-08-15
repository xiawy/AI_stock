"""Supply-demand gap quantification model.

For each scored news item, this module quantifies the supply-demand dynamics:
- Demand change (increase/decrease/neutral)
- Supply change (increase/decrease/neutral)
- Gap type (shortage / surplus / balanced)
- Elasticity coefficient
- Expected gain range

Uses LLM to extract supply-demand signals from news text, then applies a
rule-based matrix for classification.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from . import evolution as pipeline_evolution

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schema
# ---------------------------------------------------------------------------


class SupplyDemandResult(BaseModel):
    """Supply-demand analysis for a single news item."""

    demand_change: str = Field(
        default="neutral",
        description="demand_increase / demand_decrease / neutral",
    )
    supply_change: str = Field(
        default="neutral",
        description="supply_increase / supply_decrease / neutral",
    )
    gap_type: str = Field(
        default="balanced",
        description="shortage / surplus / balanced / demand_shortage / supply_surplus",
    )
    elasticity_coefficient: float = Field(
        default=0.5,
        description="0.0-1.0: how strongly the gap translates to price movement",
    )
    expected_gain_low: float = Field(
        default=0.0,
        description="Expected minimum gain (%) from this supply-demand gap",
    )
    expected_gain_high: float = Field(
        default=0.0,
        description="Expected maximum gain (%) from this supply-demand gap",
    )
    reasoning: str = Field(
        default="",
        description="Brief explanation of the supply-demand analysis",
    )


# ---------------------------------------------------------------------------
# Gap classification matrix
# ---------------------------------------------------------------------------

# Demand change × Supply change → Gap type
_GAP_MATRIX: dict[tuple[str, str], str] = {
    ("demand_increase", "supply_decrease"): "shortage",       # 供不应求
    ("demand_increase", "supply_neutral"): "demand_shortage",  # 需求增加供给不变
    ("demand_increase", "supply_increase"): "balanced",        # 供需同增
    ("demand_decrease", "supply_increase"): "surplus",         # 供过于求
    ("demand_decrease", "supply_neutral"): "surplus",          # 需求减少供给不变
    ("demand_decrease", "supply_decrease"): "balanced",        # 供需同减
    ("demand_neutral", "supply_decrease"): "supply_surplus",   # 供给减少需求不变 → 偏利好
    ("demand_neutral", "supply_increase"): "surplus",          # 供给增加需求不变
    ("demand_neutral", "supply_neutral"): "balanced",          # 无变化
}

# Normalize agent signal names to our demand/supply change vocabulary
_SIGNAL_MAP: dict[str, str] = {
    "demand_surge": "demand_increase",
    "demand_shrink": "demand_decrease",
    "supply_shrink": "supply_decrease",
    "supply_surge": "supply_increase",
    "both_surge": "both_increase",
    "both_shrink": "both_decrease",
    "neutral": "neutral",
}

# Expected gain ranges by gap type (low%, high%)
_GAIN_RANGES: dict[str, tuple[float, float]] = {
    "shortage": (5.0, 15.0),       # 供不应求 → 强利好
    "demand_shortage": (3.0, 10.0),  # 需求增加
    "supply_surplus": (2.0, 8.0),   # 供给收缩（偏利好）
    "surplus": (-5.0, 2.0),         # 供过于求 → 利空
    "balanced": (-1.0, 1.0),        # 供需平衡
}


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

_SUPPLY_DEMAND_PROMPT = """你是一位供需分析专家，专注于从新闻中提取供需变化信号。

请分析以下新闻对相关行业供需关系的影响：

---
标题：{title}
内容：{content}
来源：{source}
---

请判断：
1. 需求端变化：demand_increase / demand_decrease / neutral
2. 供给端变化：supply_increase / supply_decrease / neutral
3. 缺口类型：shortage / surplus / balanced / demand_shortage / supply_surplus
4. 弹性系数：0.0-1.0（缺口转化为价格波动的强度）
5. 预期涨幅区间：基于供需缺口的预期涨幅范围（%）

请以 JSON 格式输出：
{{"demand_change": "...", "supply_change": "...", "gap_type": "...", "elasticity_coefficient": 0.5, "expected_gain_low": 0.0, "expected_gain_high": 0.0, "reasoning": "..."}}
"""


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def quantify_supply_demand(
    news_item: dict,
    agent_scores: list[str],
    llm: Any,
) -> SupplyDemandResult:
    """Quantify supply-demand gap for a single news item.

    Args:
        news_item: The news dict with title, content, source, etc.
        agent_scores: List of supply_demand_signal strings from the 4 agents.
        llm: LLM instance for analysis.

    Returns:
        SupplyDemandResult with gap analysis.
    """
    # First, try to derive from agent signals (no LLM call needed)
    derived = _derive_from_agent_signals(agent_scores)

    # Wrap LLM with evolution context (custom strategies + past episodes)
    evo_llm = pipeline_evolution.wrap_llm(llm, "pipeline_supply_demand")

    # Then enhance with LLM analysis
    try:
        llm_result = _llm_analyze(news_item, evo_llm)
        # Merge: LLM result takes precedence for gap_type, but we keep
        # the agent-derived elasticity as a sanity check
        result = _merge_results(derived, llm_result)
    except Exception as exc:
        logger.debug("LLM supply-demand analysis failed: %s; using agent-derived", exc)
        result = derived

    return result


def batch_quantify_supply_demand(
    news_items: list[dict],
    agent_scores_map: dict[str, list[str]],
    llm: Any,
) -> list[SupplyDemandResult]:
    """Quantify supply-demand for multiple news items.

    Args:
        news_items: List of news dicts.
        agent_scores_map: Mapping from title_hash to list of agent signals.
        llm: LLM instance.

    Returns:
        List of SupplyDemandResult, one per news item.
    """
    results = []
    for news in news_items:
        th = news.get("title_hash", "")
        signals = agent_scores_map.get(th, ["neutral"] * 4)
        result = quantify_supply_demand(news, signals, llm)
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _derive_from_agent_signals(signals: list[str]) -> SupplyDemandResult:
    """Derive supply-demand result from the 4 agents' signals."""
    # Count signal types
    demand_signals = []
    supply_signals = []

    for signal in signals:
        normalized = _SIGNAL_MAP.get(signal, "neutral")
        if normalized in ("both_increase",):
            demand_signals.append("demand_increase")
            supply_signals.append("supply_increase")
        elif normalized in ("both_decrease",):
            demand_signals.append("demand_decrease")
            supply_signals.append("supply_decrease")
        elif normalized.startswith("demand_"):
            demand_signals.append(normalized)
        elif normalized.startswith("supply_"):
            supply_signals.append(normalized)
        # "neutral" goes to neither

    # Majority vote for demand and supply
    demand_change = _majority_vote(demand_signals, "neutral")
    supply_change = _majority_vote(supply_signals, "neutral")

    # Map to our gap matrix keys
    demand_key = demand_change if demand_change in ("demand_increase", "demand_decrease") else "demand_neutral"
    supply_key = supply_change if supply_change in ("supply_increase", "supply_decrease") else "supply_neutral"

    gap_type = _GAP_MATRIX.get((demand_key, supply_key), "balanced")

    # Calculate elasticity based on signal agreement
    agreement = sum(1 for s in signals if s != "neutral") / max(len(signals), 1)
    elasticity = min(1.0, agreement * 1.5)  # Scale up but cap at 1.0

    # Expected gain range
    gain_low, gain_high = _GAIN_RANGES.get(gap_type, (-1.0, 1.0))
    # Scale by elasticity
    gain_low *= elasticity
    gain_high *= elasticity

    return SupplyDemandResult(
        demand_change=demand_change,
        supply_change=supply_change,
        gap_type=gap_type,
        elasticity_coefficient=round(elasticity, 2),
        expected_gain_low=round(gain_low, 2),
        expected_gain_high=round(gain_high, 2),
        reasoning=f"基于 {len(signals)} 个 Agent 信号推导: {', '.join(signals)}",
    )


def _majority_vote(signals: list[str], default: str) -> str:
    """Return the most common signal, or default if empty."""
    if not signals:
        return default
    from collections import Counter
    counts = Counter(signals)
    return counts.most_common(1)[0][0]


def _llm_analyze(news_item: dict, llm: Any) -> SupplyDemandResult:
    """Use LLM to analyze supply-demand dynamics from news text."""
    title = news_item.get("title", "")
    content = news_item.get("content", "")[:500]
    source = news_item.get("source", "")

    prompt = _SUPPLY_DEMAND_PROMPT.format(
        title=title, content=content, source=source,
    )

    # Try structured output
    try:
        structured_llm = llm.with_structured_output(SupplyDemandResult)
        result = structured_llm.invoke(prompt)
        if isinstance(result, SupplyDemandResult):
            return result
        return SupplyDemandResult.model_validate(result)
    except Exception:
        pass

    # Fallback: free-text + JSON parse
    response = llm.invoke(prompt)
    text = response.content if hasattr(response, "content") else str(response)
    return _parse_supply_demand_text(text)


def _parse_supply_demand_text(text: str) -> SupplyDemandResult:
    """Parse supply-demand result from free-text LLM output."""
    import re

    # Try JSON extraction
    json_match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group())
            return SupplyDemandResult.model_validate(data)
        except Exception:
            pass

    return SupplyDemandResult(
        reasoning=text[:200] if text else "无法解析供需分析",
    )


def _merge_results(
    derived: SupplyDemandResult,
    llm_result: SupplyDemandResult,
) -> SupplyDemandResult:
    """Merge agent-derived and LLM-analyzed results.

    LLM takes precedence for gap_type and reasoning; we average the elasticity.
    """
    # Use LLM's gap_type if it's not "balanced" (more informative)
    gap_type = llm_result.gap_type if llm_result.gap_type != "balanced" else derived.gap_type

    # Average elasticity
    elasticity = (derived.elasticity_coefficient + llm_result.elasticity_coefficient) / 2

    # Use LLM's gain range if available, otherwise derive from merged gap
    if llm_result.expected_gain_high > 0:
        gain_low = llm_result.expected_gain_low
        gain_high = llm_result.expected_gain_high
    else:
        gain_low, gain_high = _GAIN_RANGES.get(gap_type, (-1.0, 1.0))
        gain_low *= elasticity
        gain_high *= elasticity

    return SupplyDemandResult(
        demand_change=llm_result.demand_change or derived.demand_change,
        supply_change=llm_result.supply_change or derived.supply_change,
        gap_type=gap_type,
        elasticity_coefficient=round(elasticity, 2),
        expected_gain_low=round(gain_low, 2),
        expected_gain_high=round(gain_high, 2),
        reasoning=llm_result.reasoning or derived.reasoning,
    )
