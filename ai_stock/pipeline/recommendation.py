"""Final stock recommendation — Top 10 + 3 alternates.

Combines the 3-dimensional scoring with debate results to produce
the final recommendation list.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel

from .config import (
    FINAL_DEBATE_WEIGHT,
    FINAL_EVENT_MATCH_WEIGHT,
    FINAL_FUNDAMENTALS_WEIGHT,
    FINAL_TECHNICAL_WEIGHT,
    TOP_N_ALTERNATES,
    TOP_N_RECOMMENDED,
)
from .stock_debate import StockDebateResult
from .stock_scoring import StockScoreResult
from . import evolution as pipeline_evolution

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schema for LLM detail generation
# ---------------------------------------------------------------------------


class _DetailSchema(BaseModel):
    """LLM-generated recommendation details."""
    industry: str = ""
    buy_logic: str = ""
    target_price: float = 0.0
    expected_gain_low: float = 0.0
    expected_gain_high: float = 0.0
    stop_loss_price: float = 0.0
    holding_period: str = "短线"
    risk_level: str = "中"


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------


class FinalRecommendation:
    """A single stock recommendation with all details."""

    __slots__ = (
        "ticker", "stock_name", "industry", "trigger_event", "buy_logic",
        "fundamentals_score", "technical_score", "event_match_score",
        "debate_score", "final_score",
        "target_price", "expected_gain_low", "expected_gain_high",
        "stop_loss_price", "holding_period", "risk_level",
        "bull_bear_summary", "rank", "is_alternate",
    )

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot, ""))

    def to_dict(self) -> dict:
        return {slot: getattr(self, slot) for slot in self.__slots__}


# ---------------------------------------------------------------------------
# Main recommendation function
# ---------------------------------------------------------------------------


def generate_recommendation(
    stock_scores: list[StockScoreResult],
    debate_results: list[StockDebateResult],
    events: list[dict],
    llm: Any,
) -> list[FinalRecommendation]:
    """Generate the final Top 10 + 3 alternates recommendation.

    Args:
        stock_scores: Scored stocks from score_all_candidates.
        debate_results: Debate results from debate_batch_stocks.
        events: Top events for context.
        llm: LLM for generating buy logic and price targets.

    Returns:
        List of FinalRecommendation, ranked 1-13 (1-10 primary, 11-13 alternates).
    """
    if not stock_scores or not debate_results:
        logger.warning("No stocks or debate results to generate recommendation")
        return []

    # Ensure lengths match
    n = min(len(stock_scores), len(debate_results))
    stock_scores = stock_scores[:n]
    debate_results = debate_results[:n]

    # Calculate final scores
    scored_stocks: list[tuple[StockScoreResult, StockDebateResult, float]] = []
    for stock, debate in zip(stock_scores, debate_results):
        # Final scoring: adjusted weights after debate
        final = (
            FINAL_FUNDAMENTALS_WEIGHT * stock.fundamentals.score
            + FINAL_TECHNICAL_WEIGHT * stock.technical.score
            + FINAL_EVENT_MATCH_WEIGHT * stock.event_match.score
            + FINAL_DEBATE_WEIGHT * debate.debate_score
        )

        # Disqualify: debate strongly bearish
        if debate.bull_bear_bias == "bearish" and debate.debate_score < 35:
            logger.info(
                "Disqualified %s (%s): debate strongly bearish (%.1f)",
                stock.ticker, stock.stock_name, debate.debate_score,
            )
            continue

        scored_stocks.append((stock, debate, final))

    # Sort by final score desc
    scored_stocks.sort(key=lambda x: x[2], reverse=True)

    # Generate recommendations
    total_needed = TOP_N_RECOMMENDED + TOP_N_ALTERNATES
    recommendations: list[FinalRecommendation] = []

    for rank_idx, (stock, debate, final_score) in enumerate(scored_stocks[:total_needed]):
        is_alternate = rank_idx >= TOP_N_RECOMMENDED
        rank = rank_idx + 1

        # Determine trigger event
        trigger_event = _find_trigger_event(stock, events)

        # Generate buy logic and price targets via LLM
        try:
            details = _generate_details(stock, debate, trigger_event, llm)
        except Exception as exc:
            logger.warning("Detail generation failed for %s: %s", stock.ticker, exc)
            details = _default_details(stock, final_score)

        rec = FinalRecommendation(
            ticker=stock.ticker,
            stock_name=stock.stock_name,
            industry=details.get("industry", ""),
            trigger_event=trigger_event,
            buy_logic=details.get("buy_logic", ""),
            fundamentals_score=round(stock.fundamentals.score, 1),
            technical_score=round(stock.technical.score, 1),
            event_match_score=round(stock.event_match.score, 1),
            debate_score=round(debate.debate_score, 1),
            final_score=round(final_score, 1),
            target_price=details.get("target_price", 0.0),
            expected_gain_low=details.get("expected_gain_low", 0.0),
            expected_gain_high=details.get("expected_gain_high", 0.0),
            stop_loss_price=details.get("stop_loss_price", 0.0),
            holding_period=details.get("holding_period", "短线"),
            risk_level=details.get("risk_level", "中"),
            bull_bear_summary=debate.bull_bear_summary,
            rank=rank,
            is_alternate=is_alternate,
        )
        recommendations.append(rec)

    # Log if we don't have enough candidates
    if len(recommendations) < TOP_N_RECOMMENDED:
        logger.warning(
            "Only %d candidates passed all filters (need %d). "
            "Reason: insufficient candidates or too many bearish debates.",
            len(recommendations), TOP_N_RECOMMENDED,
        )

    logger.info(
        "Generated %d recommendations (%d primary + %d alternates) "
        "from %d candidates",
        len(recommendations),
        sum(1 for r in recommendations if not r.is_alternate),
        sum(1 for r in recommendations if r.is_alternate),
        len(scored_stocks),
    )

    return recommendations


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_trigger_event(stock: StockScoreResult, events: list[dict]) -> str:
    """Find the most relevant trigger event for a stock."""
    # Try to match from event_match reasoning
    reasoning = stock.event_match.reasoning
    for event in events[:5]:
        title = event.get("title", "")
        if title and title in reasoning:
            return title

    # Fall back to the first event
    if events:
        return events[0].get("title", "")
    return ""


def _generate_details(
    stock: StockScoreResult,
    debate: StockDebateResult,
    trigger_event: str,
    llm: Any,
) -> dict:
    """Use LLM to generate buy logic, target price, etc."""
    # Wrap LLM with evolution context (custom strategies + past episodes)
    evo_llm = pipeline_evolution.wrap_llm(llm, "pipeline_recommendation")

    prompt = (
        f"你是一位A股投资顾问。请为以下股票生成投资建议。\n\n"
        f"## 股票信息\n"
        f"代码：{stock.ticker}\n"
        f"名称：{stock.stock_name}\n\n"
        f"## 评分\n"
        f"- 基本面：{stock.fundamentals.score:.0f}/100\n"
        f"- 技术面：{stock.technical.score:.0f}/100\n"
        f"- 事件匹配度：{stock.event_match.score:.0f}/100\n"
        f"- 辩论得分：{debate.debate_score:.0f}/100\n"
        f"- 综合评分：{stock.composite:.1f}/100\n\n"
        f"## 触发事件\n{trigger_event}\n\n"
        f"## 多头论点\n{debate.bull_bear_summary[:200]}\n\n"
        f"请输出以下信息（JSON 格式）：\n"
        f'{{"industry": "所属行业", "buy_logic": "买入逻辑（2-3句话）", '
        f'"target_price": 目标价(float), "expected_gain_low": 预期最低涨幅(float), '
        f'"expected_gain_high": 预期最高涨幅(float), "stop_loss_price": 止损价(float), '
        f'"holding_period": "短线/中线", "risk_level": "高/中/低"}}\n'
        f"注意：如果没有当前价格数据，target_price 和 stop_loss_price 可以设为 0。"
    )

    # Try structured output
    try:
        structured_llm = evo_llm.with_structured_output(_DetailSchema)
        result = structured_llm.invoke(prompt)
        if isinstance(result, _DetailSchema):
            return result.model_dump()
        if isinstance(result, dict):
            return result
        return _DetailSchema.model_validate(result).model_dump()
    except Exception:
        pass

    # Fallback: free-text
    try:
        response = evo_llm.invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        import re
        json_match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            return data
    except Exception:
        pass

    return _default_details(stock, stock.composite)


def _default_details(stock: StockScoreResult, final_score: float) -> dict:
    """Generate default details when LLM fails."""
    # Determine risk level based on scores
    if final_score >= 75:
        risk = "低"
        holding = "中线"
    elif final_score >= 60:
        risk = "中"
        holding = "短线"
    else:
        risk = "高"
        holding = "短线"

    # Estimate gain range from event match elasticity
    elasticity = stock.event_match.elasticity
    gain_low = round(elasticity * 3, 1)
    gain_high = round(elasticity * 10, 1)

    return {
        "industry": "",
        "buy_logic": f"综合评分 {final_score:.0f}/100，事件驱动+基本面支撑",
        "target_price": 0.0,
        "expected_gain_low": gain_low,
        "expected_gain_high": gain_high,
        "stop_loss_price": 0.0,
        "holding_period": holding,
        "risk_level": risk,
    }
