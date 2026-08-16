"""LLM structured-output wrappers for news scoring.

Each of the 4 scoring agents (policy / news / capital / sentiment) shares the
same call pattern: send a news item, get back a structured score + reasoning.
This module centralises the Pydantic schemas, prompt templates, and the
structured-or-fallback invocation.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from .config import SCORING_BATCH_SIZE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class AgentScoreResult(BaseModel):
    """Single agent's evaluation of a single news item."""

    score: float = Field(
        description="Impact score from 1 to 10. Use decimals for nuance."
    )
    reasoning: str = Field(
        description="Concise reasoning (2-4 sentences) for the score."
    )
    primary_industry: str = Field(
        default="",
        description=(
            "主要受益行业（申万一级行业口径，如 电子/计算机/医药生物）。"
            "若新闻与行业无关则留空。"
        ),
    )
    secondary_industry: str = Field(
        default="",
        description=(
            "次要受益行业（申万一级行业口径）。最多 1 个，无则留空。"
        ),
    )
    industries: list[str] = Field(
        default_factory=list,
        description="List of industry/sector names affected by this news.",
    )
    top_stocks: list[dict] = Field(
        default_factory=list,
        description=(
            "Up to 3 most elastic stocks. Each dict: "
            '{"code": "000001", "name": "平安银行", "elasticity": 0.8}'
        ),
    )
    supply_demand_signal: str = Field(
        default="neutral",
        description=(
            "One of: supply_shrink, demand_surge, both_surge, both_shrink, "
            "demand_shrink, supply_surge, neutral."
        ),
    )


class BatchScoreResult(BaseModel):
    """Batch scoring result for multiple news items."""

    items: list[AgentScoreResult] = Field(
        default_factory=list,
        description="One result per news item, in the same order as input.",
    )


# ---------------------------------------------------------------------------
# Prompt templates per agent role
# ---------------------------------------------------------------------------

_AGENT_PROMPTS: dict[str, str] = {
    "policy": (
        "你是一位资深政策分析师，专注于中国A股市场的政策影响评估。\n"
        "请评估以下新闻/政策的**政策影响力**，并判断其受益行业（申万一级行业口径）。\n\n"
        "评分标准（1-10）：\n"
        "- 9-10: 国务院/央行级别的重大政策转向（降准降息、重大监管变化）\n"
        "- 7-8: 部委级产业政策，明确利好/利空特定行业\n"
        "- 5-6: 地方性政策或行业细则，影响范围有限\n"
        "- 3-4: 常规政策延续，无超预期内容\n"
        "- 1-2: 政策信号微弱或仅为例行公告\n\n"
    ),
    "news": (
        "你是一位资深财经新闻分析师，专注于评估新闻对A股市场的重要性。\n"
        "请评估以下新闻的**新闻重要性**，并判断其受益行业（申万一级行业口径）。\n\n"
        "评分标准（1-10）：\n"
        "- 9-10: 突发重大事件（黑天鹅、重大并购、行业巨变）\n"
        "- 7-8: 行业级重要事件，将显著影响相关板块走势\n"
        "- 5-6: 公司级重要事件，影响特定个股\n"
        "- 3-4: 一般性行业新闻，市场已有预期\n"
        "- 1-2: 日常资讯，对市场几乎无影响\n\n"
    ),
    "capital": (
        "你是一位资深游资追踪分析师，专注于评估新闻对短线资金吸引力的影响。\n"
        "请评估以下新闻对**游资/短线资金的吸引力**，并判断资金最可能涌入的行业（申万一级行业口径）。\n\n"
        "评分标准（1-10）：\n"
        "- 9-10: 极强题材催化，必然引发游资抢筹（如重大题材、龙头利好）\n"
        "- 7-8: 强题材，大概率有游资参与炒作\n"
        "- 5-6: 中等题材，可能吸引部分游资关注\n"
        "- 3-4: 题材偏弱，游资兴趣不大\n"
        "- 1-2: 无题材价值，游资不会关注\n\n"
    ),
    "sentiment": (
        "你是一位资深市场舆情分析师，专注于评估新闻对市场情绪的影响。\n"
        "请评估以下新闻对**市场情绪的影响力**，并判断情绪最集中的受益行业（申万一级行业口径）。\n\n"
        "评分标准（1-10）：\n"
        "- 9-10: 引发全市场情绪剧变（极度乐观/恐慌）\n"
        "- 7-8: 显著影响板块情绪，引发资金集中流入/流出\n"
        "- 5-6: 对局部情绪有影响，但不会引发大规模情绪波动\n"
        "- 3-4: 情绪影响有限，市场反应平淡\n"
        "- 1-2: 几乎不影响市场情绪\n\n"
    ),
}


def _format_news_for_prompt(news_item: dict) -> str:
    """Format a single news item for the prompt."""
    title = news_item.get("title", "")
    content = news_item.get("content", "")[:300]
    source = news_item.get("source", "")
    pub_time = news_item.get("time", "")
    category = news_item.get("category", "news")

    parts = [f"【标题】{title}"]
    if content and content != title:
        parts.append(f"【摘要】{content}")
    if source:
        parts.append(f"【来源】{source}")
    if pub_time:
        parts.append(f"【时间】{pub_time}")
    parts.append(f"【类型】{'政策' if category == 'policy' else '资讯'}")
    return "\n".join(parts)


def _build_single_prompt(agent_role: str, news_item: dict) -> str:
    """Build the prompt for scoring a single news item."""
    base = _AGENT_PROMPTS.get(agent_role, _AGENT_PROMPTS["news"])
    news_text = _format_news_for_prompt(news_item)
    return (
        f"{base}"
        f"---\n"
        f"{news_text}\n"
        f"---\n\n"
        f"请按以下要求输出：\n"
        f"1. score: 1-10 的评分\n"
        f"2. reasoning: 2-4 句评分理由\n"
        f"3. primary_industry: 最主要受益行业（申万一级行业名称，如“电子”）；"
        f"每条新闻都必须尽力填写，仅当与任何行业完全无关时才留空\n"
        f"4. secondary_industry: 次要受益行业（申万一级行业名称，最多 1 个，无则空字符串）\n"
        f"5. industries: 受影响的行业列表\n"
        f"6. top_stocks: 最多 3 支弹性最大的个股（code, name, elasticity 0-1）\n"
        f"7. supply_demand_signal: 供需信号 "
        f"(supply_shrink/demand_surge/both_surge/both_shrink/demand_shrink/supply_surge/neutral)\n"
    )


def _build_batch_prompt(agent_role: str, news_batch: list[dict]) -> str:
    """Build the prompt for scoring a batch of news items."""
    base = _AGENT_PROMPTS.get(agent_role, _AGENT_PROMPTS["news"])
    items_text = []
    for i, news_item in enumerate(news_batch, 1):
        items_text.append(f"--- 新闻 #{i} ---\n{_format_news_for_prompt(news_item)}")

    all_items = "\n\n".join(items_text)
    return (
        f"{base}"
        f"请依次评估以下 {len(news_batch)} 条新闻，每条都给出评分。\n\n"
        f"{all_items}\n\n"
        f"---\n"
        f"请对每条新闻按顺序输出同样的字段（score, reasoning, primary_industry, "
        f"secondary_industry, industries, top_stocks, supply_demand_signal），"
        f"以 JSON 数组格式返回。\n"
        f"注意：primary_industry / secondary_industry 是行业榜聚合的关键字段，"
        f"必须尽力填写（申万一级行业口径，如 电子/计算机/机械设备），"
        f"仅当新闻与任何行业完全无关时才留空；不要只填 industries 而留空这两个字段。\n"
    )


# ---------------------------------------------------------------------------
# Scoring functions
# ---------------------------------------------------------------------------


def score_news_with_agent(
    agent_role: str,
    news_item: dict,
    llm: Any,
) -> AgentScoreResult:
    """Score a single news item with a single agent.

    Attempts structured output first; falls back to free-text + JSON parse.
    """
    prompt = _build_single_prompt(agent_role, news_item)

    # Try structured output
    try:
        structured_llm = llm.with_structured_output(AgentScoreResult)
        result = structured_llm.invoke(prompt)
        if isinstance(result, AgentScoreResult):
            return result
        # Some providers return dict-like
        return AgentScoreResult.model_validate(result)
    except Exception as exc:
        logger.debug(
            "Structured output failed for %s agent: %s; trying free-text",
            agent_role, exc,
        )

    # Fallback: free-text + manual JSON extraction
    try:
        response = llm.invoke(prompt)
        content = response.content if hasattr(response, "content") else str(response)
        # Try to extract JSON from the response
        return _parse_score_from_text(content)
    except Exception as exc:
        logger.warning("Agent %s scoring failed entirely: %s", agent_role, exc)
        return AgentScoreResult(score=5.0, reasoning=f"评分失败: {exc}")


def batch_score(
    agent_role: str,
    news_batch: list[dict],
    llm: Any,
) -> list[AgentScoreResult]:
    """Score a batch of news items with a single agent.

    Sends all items in one prompt to reduce LLM call count.
    Falls back to individual scoring if batch fails.
    """
    if not news_batch:
        return []

    if len(news_batch) == 1:
        return [score_news_with_agent(agent_role, news_batch[0], llm)]

    prompt = _build_batch_prompt(agent_role, news_batch)

    # Try structured output for batch
    try:
        structured_llm = llm.with_structured_output(BatchScoreResult)
        result = structured_llm.invoke(prompt)
        if isinstance(result, BatchScoreResult) and result.items:
            return result.items[:len(news_batch)]
        if isinstance(result, dict) and "items" in result:
            items = [AgentScoreResult.model_validate(i) for i in result["items"]]
            return items[:len(news_batch)]
    except Exception as exc:
        logger.debug("Batch structured output failed for %s: %s", agent_role, exc)

    # Fallback: free-text batch
    try:
        response = llm.invoke(prompt)
        content = response.content if hasattr(response, "content") else str(response)
        return _parse_batch_from_text(content, len(news_batch))
    except Exception as exc:
        logger.warning(
            "Batch scoring failed for %s (%d items): %s; falling back to individual",
            agent_role, len(news_batch), exc,
        )

    # Final fallback: score individually
    results = []
    for item in news_batch:
        results.append(score_news_with_agent(agent_role, item, llm))
    return results


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_score_from_text(text: str) -> AgentScoreResult:
    """Try to extract a score result from free-text LLM output."""
    import re

    # Try JSON extraction
    json_match = re.search(r"\{[^{}]*\"score\"[^{}]*\}", text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group())
            return AgentScoreResult.model_validate(data)
        except Exception:
            pass

    # Try to find score number
    score_match = re.search(r"(?:score|评分|分数)[：:\s]*(\d+\.?\d*)", text, re.IGNORECASE)
    score = float(score_match.group(1)) if score_match else 5.0
    score = max(1.0, min(10.0, score))

    return AgentScoreResult(
        score=score,
        reasoning=text[:200] if text else "无法解析评分",
    )


def _parse_batch_from_text(text: str, expected_count: int) -> list[AgentScoreResult]:
    """Try to extract batch results from free-text LLM output."""
    import re

    # Try JSON array extraction
    json_match = re.search(r"\[[\s\S]*?\]", text)
    if json_match:
        try:
            data = json.loads(json_match.group())
            if isinstance(data, list):
                results = []
                for item in data:
                    if isinstance(item, dict):
                        results.append(AgentScoreResult.model_validate(item))
                if results:
                    return results[:expected_count]
        except Exception:
            pass

    # Fallback: try to find individual score patterns
    results = []
    score_patterns = re.findall(
        r"(?:第?\s*\d+\s*[条.#]|新闻\s*#?\s*\d+)[：:\s]*.*?(?:score|评分)[：:\s]*(\d+\.?\d*)",
        text, re.IGNORECASE | re.DOTALL,
    )
    for s in score_patterns[:expected_count]:
        score = max(1.0, min(10.0, float(s)))
        results.append(AgentScoreResult(score=score, reasoning="从文本提取"))

    # Pad with defaults if we didn't find enough
    while len(results) < expected_count:
        results.append(AgentScoreResult(score=5.0, reasoning="批量解析失败，使用默认值"))

    return results[:expected_count]
