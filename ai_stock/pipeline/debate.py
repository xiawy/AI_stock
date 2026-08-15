"""News-level bull/bear debate for impact assessment.

Each candidate news item goes through a lightweight debate:
- Bull Researcher: builds the bullish case based on positive factors
- Bear Researcher: builds the bearish case based on risk factors
- Research Manager (deep LLM):综合判定多空倾向

This is intentionally simpler than the full stock-level debate (no LangGraph).
We reuse the prompt *style* from the existing bull/bear researchers but
adapt it for news-level granularity.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from pydantic import BaseModel, Field

from .config import DEBATE_MAX_WORKERS, NEWS_DEBATE_MAX_ROUNDS
from . import evolution as pipeline_evolution

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class DebateVerdict(BaseModel):
    """Research Manager's verdict on a news item's bull/bear bias."""

    bull_bear_bias: str = Field(
        default="neutral",
        description="bullish / bearish / neutral",
    )
    debate_summary: str = Field(
        default="",
        description="Concise summary of the debate outcome (2-4 sentences).",
    )
    confidence: float = Field(
        default=0.5,
        description="Confidence in the verdict, 0.0-1.0.",
    )


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_BULL_PROMPT = """你是一位多头研究员，需要为以下新闻/事件构建看涨逻辑。

## 新闻内容
标题：{title}
内容摘要：{content}
来源：{source}
类型：{category}

## 4 Agent 评分
- 政策影响力：{policy_score}/10
- 新闻重要性：{news_score}/10
- 游资吸引力：{capital_score}/10
- 舆情影响力：{sentiment_score}/10
- 综合评分：{composite_score}/10

## 供需分析
{supply_demand_summary}

## 影响行业
{industries}

请基于以上信息，构建看涨论据。重点关注：
1. 政策催化力度
2. 供需缺口带来的机会
3. 市场情绪发酵空间
4. 资金面支撑

用 3-5 句话阐述核心看涨逻辑。
"""

_BEAR_PROMPT = """你是一位空头研究员，需要为以下新闻/事件构建看跌逻辑。

## 新闻内容
标题：{title}
内容摘要：{content}
来源：{source}
类型：{category}

## 4 Agent 评分
- 政策影响力：{policy_score}/10
- 新闻重要性：{news_score}/10
- 游资吸引力：{capital_score}/10
- 舆情影响力：{sentiment_score}/10
- 综合评分：{composite_score}/10

## 供需分析
{supply_demand_summary}

## 影响行业
{industries}

请基于以上信息，构建看跌论据。重点关注：
1. 政策不及预期的风险
2. 供需改善可能是短暂的
3. 市场已提前定价的风险
4. 追高风险和获利盘压力

用 3-5 句话阐述核心看跌逻辑。
"""

_JUDGE_PROMPT = """你是一位研究主管，需要综合多空双方观点，判定这条新闻/事件的多空倾向。

## 新闻内容
标题：{title}
内容摘要：{content}

## 多头论点
{bull_argument}

## 空头论点
{bear_argument}

## 历史辩论记录
{debate_history}

请综合判断：
1. bull_bear_bias: bullish / bearish / neutral
2. debate_summary: 2-4 句话总结多空辩论结果
3. confidence: 判定置信度 0.0-1.0

以 JSON 格式输出：
{{"bull_bear_bias": "...", "debate_summary": "...", "confidence": 0.7}}
"""


# ---------------------------------------------------------------------------
# Debate functions
# ---------------------------------------------------------------------------


def _format_supply_demand(summary_json: str) -> str:
    """Format supply-demand JSON for display in prompts."""
    if not summary_json:
        return "无供需分析数据"
    try:
        data = json.loads(summary_json)
        if not data:
            return "无供需分析数据"
        parts = []
        gap = data.get("gap_type", "balanced")
        gap_labels = {
            "shortage": "供不应求（强利好）",
            "demand_shortage": "需求增加",
            "supply_surplus": "供给收缩",
            "surplus": "供过于求（利空）",
            "balanced": "供需平衡",
        }
        parts.append(f"缺口类型：{gap_labels.get(gap, gap)}")
        if data.get("elasticity_coefficient"):
            parts.append(f"弹性系数：{data['elasticity_coefficient']}")
        if data.get("expected_gain_high", 0) > 0:
            parts.append(
                f"预期涨幅：{data.get('expected_gain_low', 0):.1f}% ~ "
                f"{data.get('expected_gain_high', 0):.1f}%"
            )
        if data.get("reasoning"):
            parts.append(f"分析理由：{data['reasoning']}")
        return "\n".join(parts) if parts else "无供需分析数据"
    except (json.JSONDecodeError, TypeError):
        return "无供需分析数据"


def debate_news(
    news_item: dict,
    scores: dict,
    llm_quick: Any,
    llm_deep: Any,
    max_rounds: int = NEWS_DEBATE_MAX_ROUNDS,
) -> DebateVerdict:
    """Run a bull/bear debate on a single news item.

    Args:
        news_item: News dict with title, content, source, category, etc.
        scores: Dict with agent scores and supply_demand_json.
        llm_quick: LLM for bull/bear researchers.
        llm_deep: LLM for research manager (judge).
        max_rounds: Maximum debate rounds.

    Returns:
        DebateVerdict with bias, summary, and confidence.
    """
    title = news_item.get("title", "")
    content = news_item.get("content", "")[:300]
    source = news_item.get("source", "")
    category = news_item.get("category", "news")

    # Format scores for prompts
    policy_score = scores.get("policy_score", 5.0)
    news_score = scores.get("news_score", 5.0)
    capital_score = scores.get("capital_score", 5.0)
    sentiment_score = scores.get("sentiment_score", 5.0)
    composite_score = scores.get("composite_score", 5.0)

    supply_demand_summary = _format_supply_demand(
        scores.get("supply_demand_json", "")
    )
    industries = ", ".join(scores.get("industries", [])) or "未明确"

    debate_history = ""
    bull_arg = ""
    bear_arg = ""

    # Wrap LLMs with per-role evolution context (custom strategies + past episodes)
    evo_bull = pipeline_evolution.wrap_llm(llm_quick, "pipeline_bull")
    evo_bear = pipeline_evolution.wrap_llm(llm_quick, "pipeline_bear")
    evo_judge = pipeline_evolution.wrap_llm(llm_deep, "pipeline_research_manager")

    for round_num in range(max_rounds):
        # Bull argument
        bull_prompt = _BULL_PROMPT.format(
            title=title, content=content, source=source, category=category,
            policy_score=policy_score, news_score=news_score,
            capital_score=capital_score, sentiment_score=sentiment_score,
            composite_score=composite_score,
            supply_demand_summary=supply_demand_summary,
            industries=industries,
        )
        if debate_history:
            bull_prompt += (
                f"\n\n## 前轮辩论记录\n{debate_history}\n"
                f"请针对空头在上一轮提出的看跌论据进行逐点反驳，说明为什么这些风险不成立或被夸大。"
            )

        try:
            bull_response = evo_bull.invoke(bull_prompt)
            bull_arg = bull_response.content if hasattr(bull_response, "content") else str(bull_response)
        except Exception as exc:
            logger.warning("Bull debate failed for '%s': %s", title[:30], exc)
            bull_arg = "（多头未能构建有效论点）"

        # Bear argument — always sees bull's current-round argument
        bear_prompt = _BEAR_PROMPT.format(
            title=title, content=content, source=source, category=category,
            policy_score=policy_score, news_score=news_score,
            capital_score=capital_score, sentiment_score=sentiment_score,
            composite_score=composite_score,
            supply_demand_summary=supply_demand_summary,
            industries=industries,
        )
        bear_prompt += f"\n\n## 多头本轮论点\n{bull_arg}\n"
        if round_num > 0 and debate_history:
            bear_prompt += f"## 前轮辩论记录\n{debate_history}\n"
        bear_prompt += "请针对多头的论点进行逐点反驳。"

        try:
            bear_response = evo_bear.invoke(bear_prompt)
            bear_arg = bear_response.content if hasattr(bear_response, "content") else str(bear_response)
        except Exception as exc:
            logger.warning("Bear debate failed for '%s': %s", title[:30], exc)
            bear_arg = "（空头未能构建有效论点）"

        debate_history += f"\n--- 第 {round_num + 1} 轮 ---\n多头：{bull_arg}\n空头：{bear_arg}\n"

    # Judge: Research Manager (deep LLM)
    judge_prompt = _JUDGE_PROMPT.format(
        title=title, content=content[:200],
        bull_argument=bull_arg, bear_argument=bear_arg,
        debate_history=debate_history,
    )

    # Try structured output
    try:
        structured_llm = evo_judge.with_structured_output(DebateVerdict)
        result = structured_llm.invoke(judge_prompt)
        if isinstance(result, DebateVerdict):
            return result
        return DebateVerdict.model_validate(result)
    except Exception:
        pass

    # Fallback: free-text
    try:
        response = evo_judge.invoke(judge_prompt)
        text = response.content if hasattr(response, "content") else str(response)
        return _parse_verdict(text)
    except Exception as exc:
        logger.warning("Judge debate failed for '%s': %s", title[:30], exc)
        return DebateVerdict(
            bull_bear_bias="neutral",
            debate_summary="辩论失败",
            confidence=0.0,
        )


def debate_batch(
    candidate_news: list[tuple[dict, dict]],
    llm_quick: Any,
    llm_deep: Any,
) -> list[DebateVerdict]:
    """Run debates on multiple news items in parallel.

    Args:
        candidate_news: List of (news_item, scores) tuples.
        llm_quick: LLM for bull/bear.
        llm_deep: LLM for judge.

    Returns:
        List of DebateVerdict, one per news item.
    """
    if not candidate_news:
        return []

    results: list[DebateVerdict | None] = [None] * len(candidate_news)

    with ThreadPoolExecutor(max_workers=DEBATE_MAX_WORKERS) as executor:
        future_to_idx = {}
        for idx, (news, scores) in enumerate(candidate_news):
            future = executor.submit(debate_news, news, scores, llm_quick, llm_deep)
            future_to_idx[future] = idx

        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                logger.error("Debate failed for item %d: %s", idx, exc)
                results[idx] = DebateVerdict(
                    bull_bear_bias="neutral",
                    debate_summary=f"辩论失败: {exc}",
                    confidence=0.0,
                )

    return [r if r is not None else DebateVerdict() for r in results]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_verdict(text: str) -> DebateVerdict:
    """Parse a debate verdict from free-text LLM output."""
    import re

    # Try JSON extraction
    json_match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group())
            return DebateVerdict.model_validate(data)
        except Exception:
            pass

    # Try to extract bias
    bias = "neutral"
    bias_match = re.search(
        r"(?:bull_bear_bias|多空倾向|bias)[：:\s]*(bullish|bearish|neutral|看多|看空|中性)",
        text, re.IGNORECASE,
    )
    if bias_match:
        val = bias_match.group(1).lower()
        if val in ("bullish", "看多"):
            bias = "bullish"
        elif val in ("bearish", "看空"):
            bias = "bearish"

    return DebateVerdict(
        bull_bear_bias=bias,
        debate_summary=text[:200] if text else "无法解析辩论结果",
        confidence=0.5,
    )
