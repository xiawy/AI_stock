"""3-dimensional stock scoring for recommendation.

Each candidate stock is scored on:
1. Fundamentals (基本面) — 35% weight
2. Technicals (技术面) — 35% weight
3. Event match (事件匹配度) — 30% weight

The composite score determines which stocks advance to the debate stage.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from pydantic import BaseModel, Field

from .config import (
    EVENT_MATCH_WEIGHT,
    FUNDAMENTALS_WEIGHT,
    TECHNICAL_WEIGHT,
    SCORING_MAX_WORKERS,
)
from . import evolution as pipeline_evolution

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class FundamentalsScore(BaseModel):
    """Fundamentals evaluation result (0-100)."""

    score: float = Field(default=50.0, description="Overall score 0-100")
    growth: float = Field(default=50.0, description="Growth sub-score (revenue/profit growth)")
    profitability: float = Field(default=50.0, description="Profitability sub-score (ROE, margin)")
    valuation: float = Field(default=50.0, description="Valuation sub-score (PE, PB)")
    safety: float = Field(default=50.0, description="Safety sub-score (debt, cashflow)")
    reasoning: str = Field(default="", description="Brief reasoning")


class TechnicalScore(BaseModel):
    """Technical evaluation result (0-100)."""

    score: float = Field(default=50.0, description="Overall score 0-100")
    trend: float = Field(default=50.0, description="Trend sub-score (MA alignment)")
    volume: float = Field(default=50.0, description="Volume sub-score")
    limit_up_quality: float = Field(default=50.0, description="Limit-up board quality")
    overbought: float = Field(default=50.0, description="Overbought/oversold (RSI)")
    fund_flow: float = Field(default=50.0, description="Fund flow sub-score")
    reasoning: str = Field(default="", description="Brief reasoning")


class EventMatchScore(BaseModel):
    """Event matching evaluation result (0-100)."""

    score: float = Field(default=50.0, description="Overall score 0-100")
    relevance: float = Field(default=50.0, description="Business relevance to events")
    elasticity: float = Field(default=0.5, description="Price elasticity to events (0-1)")
    certainty: float = Field(default=50.0, description="Benefit certainty (0-100)")
    reasoning: str = Field(default="", description="Brief reasoning")


class StockScoreResult(BaseModel):
    """Combined 3-dimensional score for a stock."""

    ticker: str
    stock_name: str = ""
    industry: str = ""
    fundamentals: FundamentalsScore = Field(default_factory=FundamentalsScore)
    technical: TechnicalScore = Field(default_factory=TechnicalScore)
    event_match: EventMatchScore = Field(default_factory=EventMatchScore)
    composite: float = Field(default=0.0, description="Weighted composite score")


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_FUNDAMENTALS_PROMPT = """你是一位A股基本面分析师。请对以下股票进行基本面评分（0-100）。

## 股票信息
代码：{ticker}
名称：{stock_name}
所属行业：{industry}

## 基本面数据
{fundamentals_data}

## 评分维度
1. 成长性（40%）：营收增长率、净利润增长率
2. 盈利能力（30%）：ROE、毛利率、净利率
3. 估值（20%）：PE、PB、PEG
4. 安全性（10%）：资产负债率、经营现金流

请以 JSON 格式输出：
{{"score": 70, "growth": 75, "profitability": 65, "valuation": 70, "safety": 60, "reasoning": "..."}}
"""

_TECHNICAL_PROMPT = """你是一位A股技术分析师。请对以下股票进行技术面评分（0-100）。

## 股票信息
代码：{ticker}
名称：{stock_name}
近期涨停情况：{limit_up_info}

## 技术数据
{technical_data}

## 评分维度
1. 趋势（25%）：均线排列、趋势方向
2. 量能（20%）：成交量变化、量价配合
3. 涨停质量（25%）：低位首板15分 / 突破板12分 / 中继板8分 / 高位加速5分
4. 超买超卖（15%）：RSI、KDJ
5. 资金流（15%）：主力净流入、龙虎榜

请以 JSON 格式输出：
{{"score": 70, "trend": 75, "volume": 65, "limit_up_quality": 70, "overbought": 60, "fund_flow": 65, "reasoning": "..."}}
"""

_EVENT_MATCH_PROMPT = """你是一位A股事件驱动分析师。请评估以下股票与利好事件的匹配度（0-100）。

## 股票信息
代码：{ticker}
名称：{stock_name}
所属行业：{industry}

## 关联事件
{events_summary}

## 评分维度
1. 相关性（40%）：主营业务与事件的直接关联程度
2. 弹性系数（30%）：股价对事件的敏感度（0-1）
3. 受益确定性（30%）：从事件到业绩兑现的确定性

请以 JSON 格式输出：
{{"score": 70, "relevance": 75, "elasticity": 0.7, "certainty": 65, "reasoning": "..."}}
"""


# ---------------------------------------------------------------------------
# Scoring functions
# ---------------------------------------------------------------------------


def score_fundamentals(
    ticker: str,
    stock_name: str,
    industry: str,
    llm: Any,
) -> FundamentalsScore:
    """Score a stock's fundamentals using LLM.

    Fetches real fundamentals data via the data layer (route_to_vendor)
    and passes it to the LLM for scoring.
    """
    # Fetch fundamentals data via the data layer
    fund_data = _fetch_fundamentals_data(ticker)

    prompt = _FUNDAMENTALS_PROMPT.format(
        ticker=ticker, stock_name=stock_name,
        industry=industry, fundamentals_data=fund_data,
    )

    return _invoke_scoring(prompt, FundamentalsScore, llm, "fundamentals")


def score_technical(
    ticker: str,
    stock_name: str,
    limit_up_info: str,
    llm: Any,
) -> TechnicalScore:
    """Score a stock's technicals using LLM."""
    tech_data = _fetch_technical_data(ticker)

    prompt = _TECHNICAL_PROMPT.format(
        ticker=ticker, stock_name=stock_name,
        limit_up_info=limit_up_info, technical_data=tech_data,
    )

    return _invoke_scoring(prompt, TechnicalScore, llm, "technical")


def score_event_match(
    ticker: str,
    stock_name: str,
    industry: str,
    events: list[dict],
    llm: Any,
) -> EventMatchScore:
    """Score how well a stock matches the trigger events."""
    events_text = []
    for e in events[:5]:
        title = e.get("title", "")
        industries = e.get("industries", [])
        if isinstance(industries, str):
            try:
                industries = json.loads(industries)
            except (json.JSONDecodeError, TypeError):
                industries = []
        events_text.append(f"- {title} | 行业: {', '.join(industries[:3])}")

    prompt = _EVENT_MATCH_PROMPT.format(
        ticker=ticker, stock_name=stock_name,
        industry=industry,
        events_summary="\n".join(events_text) if events_text else "无关联事件",
    )

    return _invoke_scoring(prompt, EventMatchScore, llm, "event_match")


def composite_score(
    fundamentals: FundamentalsScore,
    technical: TechnicalScore,
    event_match: EventMatchScore,
) -> float:
    """Calculate weighted composite score."""
    return (
        FUNDAMENTALS_WEIGHT * fundamentals.score
        + TECHNICAL_WEIGHT * technical.score
        + EVENT_MATCH_WEIGHT * event_match.score
    )


def score_all_candidates(
    candidates: list[dict],
    events: list[dict],
    llm: Any,
    top_n: int = 20,
) -> list[StockScoreResult]:
    """Score all candidates and return top N by composite.

    Each candidate's 3-dimensional scoring runs in parallel via ThreadPoolExecutor.

    Args:
        candidates: List of candidate dicts from generate_candidate_pool.
        events: Top events for event matching.
        llm: LLM instance.
        top_n: Number of top scorers to return for debate.

    Returns:
        List of StockScoreResult, sorted by composite desc.
    """

    def _score_one(cand: dict) -> StockScoreResult:
        ticker = cand.get("code", "")
        name = cand.get("name", "")
        industries = cand.get("matched_industries", [])
        industry = industries[0] if industries else ""

        try:
            fund = score_fundamentals(ticker, name, industry, llm)
        except Exception as exc:
            logger.warning("Fundamentals scoring failed for %s: %s", ticker, exc)
            fund = FundamentalsScore(reasoning=f"评分失败: {exc}")

        try:
            lu_info = f"来源层级: {cand.get('source_tier', 'P2')}"
            tech = score_technical(ticker, name, lu_info, llm)
        except Exception as exc:
            logger.warning("Technical scoring failed for %s: %s", ticker, exc)
            tech = TechnicalScore(reasoning=f"评分失败: {exc}")

        try:
            ev_match = score_event_match(ticker, name, industry, events, llm)
        except Exception as exc:
            logger.warning("Event match scoring failed for %s: %s", ticker, exc)
            ev_match = EventMatchScore(reasoning=f"评分失败: {exc}")

        comp = composite_score(fund, tech, ev_match)

        return StockScoreResult(
            ticker=ticker,
            stock_name=name,
            industry=industry,
            fundamentals=fund,
            technical=tech,
            event_match=ev_match,
            composite=round(comp, 2),
        )

    results: list[StockScoreResult] = []

    with ThreadPoolExecutor(max_workers=SCORING_MAX_WORKERS) as executor:
        future_to_cand = {
            executor.submit(_score_one, cand): cand
            for cand in candidates
        }
        for future in as_completed(future_to_cand):
            try:
                results.append(future.result())
            except Exception as exc:
                cand = future_to_cand[future]
                logger.error("Scoring failed for %s: %s", cand.get("code", ""), exc)

    # Sort by composite desc, take top N
    results.sort(key=lambda r: r.composite, reverse=True)
    return results[:top_n]


# ---------------------------------------------------------------------------
# Data fetching helpers
# ---------------------------------------------------------------------------


def _fetch_fundamentals_data(ticker: str) -> str:
    """Fetch fundamentals data string for LLM context."""
    try:
        from ai_stock.dataflows.interface import route_to_vendor
        data = route_to_vendor("get_fundamentals", ticker, None)
        if isinstance(data, str):
            return data[:1500]  # Truncate to save tokens
        return str(data)[:1500]
    except Exception as exc:
        logger.debug("Could not fetch fundamentals for %s: %s", ticker, exc)
        return "基本面数据获取失败"


def _fetch_technical_data(ticker: str) -> str:
    """Fetch technical data string for LLM context."""
    try:
        from datetime import datetime, timedelta
        from ai_stock.dataflows.interface import route_to_vendor

        today = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

        parts = []
        # Stock data (K-line)
        stock_data = route_to_vendor("get_stock_data", ticker, start, today)
        if isinstance(stock_data, str):
            parts.append(f"## K线数据\n{stock_data[:800]}")

        # Fund flow
        fund_flow = route_to_vendor("get_fund_flow", ticker, today, False)
        if isinstance(fund_flow, str):
            parts.append(f"## 资金流向\n{fund_flow[:500]}")

        return "\n\n".join(parts) if parts else "技术数据获取失败"
    except Exception as exc:
        logger.debug("Could not fetch technicals for %s: %s", ticker, exc)
        return "技术数据获取失败"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _invoke_scoring(
    prompt: str,
    schema: type,
    llm: Any,
    label: str,
) -> Any:
    """Invoke LLM scoring with structured output + fallback.

    The LLM is wrapped with evolution context (custom strategies + past episodes)
    for the corresponding agent role.
    """
    # Wrap LLM with per-dimension evolution context
    evo_llm = pipeline_evolution.wrap_llm(llm, f"pipeline_{label}")

    # Try structured output
    try:
        structured_llm = evo_llm.with_structured_output(schema)
        result = structured_llm.invoke(prompt)
        if isinstance(result, schema):
            return result
        return schema.model_validate(result)
    except Exception:
        pass

    # Fallback: free-text
    try:
        response = evo_llm.invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        return _parse_score_text(text, schema)
    except Exception as exc:
        logger.warning("%s scoring failed entirely: %s", label, exc)
        return schema(reasoning=f"评分失败: {exc}")


def _parse_score_text(text: str, schema: type) -> Any:
    """Parse scoring result from free-text LLM output."""
    import re

    json_match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group())
            return schema.model_validate(data)
        except Exception:
            pass

    # Try to find a score number
    score_match = re.search(r"(?:score|评分)[：:\s]*(\d+\.?\d*)", text, re.IGNORECASE)
    score = float(score_match.group(1)) if score_match else 50.0
    score = max(0.0, min(100.0, score))

    return schema(score=score, reasoning=text[:200] if text else "无法解析")
