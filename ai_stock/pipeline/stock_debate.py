"""Per-stock bull/bear debate for recommendation.

Each candidate stock (top 20 from scoring) goes through a debate:
- Bull: policy catalyst / supply-demand gap / earnings release / valuation repair / sentiment
- Bear: policy miss / competition / valuation overstretch / technical top / systemic risk
- Judge (deep LLM): determine bull/bear bias and debate score adjustment

The debate score feeds into the final recommendation scoring.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from pydantic import BaseModel, Field

from .config import DEBATE_MAX_WORKERS, STOCK_DEBATE_MAX_ROUNDS
from .stock_scoring import StockScoreResult
from . import evolution as pipeline_evolution

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class StockDebateResult(BaseModel):
    """Debate result for a single stock."""

    debate_score: float = Field(
        default=50.0,
        description="Debate performance score 0-100. >50 = net bullish.",
    )
    bull_bear_bias: str = Field(
        default="neutral",
        description="bullish / bearish / neutral",
    )
    bull_bear_summary: str = Field(
        default="",
        description="Concise summary of the debate (3-5 sentences).",
    )


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_STOCK_BULL_PROMPT = """你是一位多头研究员，需要为买入以下股票构建看涨逻辑。

## 股票信息
代码：{ticker}
名称：{stock_name}
行业：{industry}

## 三维评分
- 基本面：{fund_score}/100
- 技术面：{tech_score}/100
- 事件匹配度：{event_score}/100
- 综合评分：{composite_score}/100

## 关联事件
{events_summary}

请从以下维度构建看涨论据：
1. 政策催化：相关政策利好
2. 供需缺口：供不应求带来的涨价机会
3. 业绩释放：业绩增长预期
4. 估值修复：当前估值偏低
5. 情绪发酵：市场情绪正向催化

用 3-5 句话阐述核心看涨逻辑。
"""

_STOCK_BEAR_PROMPT = """你是一位空头研究员，需要为反对买入以下股票构建看跌逻辑。

## 股票信息
代码：{ticker}
名称：{stock_name}
行业：{industry}

## 三维评分
- 基本面：{fund_score}/100
- 技术面：{tech_score}/100
- 事件匹配度：{event_score}/100
- 综合评分：{composite_score}/100

## 关联事件
{events_summary}

请从以下维度构建看跌论据：
1. 政策不及预期：政策落地效果可能不如预期
2. 竞争恶化：行业竞争加剧
3. 估值透支：当前估值已充分反映利好
4. 技术见顶：短期涨幅过大，技术面超买
5. 系统性风险：大盘系统性风险

用 3-5 句话阐述核心看跌逻辑。
"""

_STOCK_JUDGE_PROMPT = """你是一位研究主管，需要综合多空双方观点，判定这只股票的投资价值。

## 股票信息
代码：{ticker}
名称：{stock_name}

## 多头论点
{bull_argument}

## 空头论点
{bear_argument}

## 辩论记录
{debate_history}

请给出：
1. debate_score: 0-100 的辩论得分（>50 偏多，<50 偏空）
2. bull_bear_bias: bullish / bearish / neutral
3. bull_bear_summary: 3-5 句话总结辩论结果

以 JSON 格式输出：
{{"debate_score": 65, "bull_bear_bias": "bullish", "bull_bear_summary": "..."}}
"""


# ---------------------------------------------------------------------------
# Debate functions
# ---------------------------------------------------------------------------


def debate_stock(
    stock_result: StockScoreResult,
    events: list[dict],
    llm_quick: Any,
    llm_deep: Any,
    max_rounds: int = STOCK_DEBATE_MAX_ROUNDS,
) -> StockDebateResult:
    """Run a bull/bear debate on a single stock.

    Args:
        stock_result: The stock's 3-dimensional scoring result.
        events: Related events for context.
        llm_quick: LLM for bull/bear researchers.
        llm_deep: LLM for judge.
        max_rounds: Maximum debate rounds.

    Returns:
        StockDebateResult with debate score and summary.
    """
    ticker = stock_result.ticker
    stock_name = stock_result.stock_name

    # Build events summary
    events_text = []
    for e in events[:5]:
        events_text.append(f"- {e.get('title', '')}")
    events_summary = "\n".join(events_text) if events_text else "无关联事件"

    # Use industry from the scored result
    industry = stock_result.industry

    debate_history = ""
    bull_arg = ""
    bear_arg = ""

    # Wrap LLMs with per-role evolution context (custom strategies + past episodes)
    evo_bull = pipeline_evolution.wrap_llm(llm_quick, "pipeline_stock_bull")
    evo_bear = pipeline_evolution.wrap_llm(llm_quick, "pipeline_stock_bear")
    evo_judge = pipeline_evolution.wrap_llm(llm_deep, "pipeline_stock_judge")

    for round_num in range(max_rounds):
        # Bull argument
        bull_prompt = _STOCK_BULL_PROMPT.format(
            ticker=ticker, stock_name=stock_name, industry=industry,
            fund_score=round(stock_result.fundamentals.score, 1),
            tech_score=round(stock_result.technical.score, 1),
            event_score=round(stock_result.event_match.score, 1),
            composite_score=round(stock_result.composite, 1),
            events_summary=events_summary,
        )
        if debate_history:
            bull_prompt += (
                f"\n\n## 前轮辩论\n{debate_history}\n"
                f"请针对空头在上一轮提出的看跌论据进行逐点反驳。"
            )

        try:
            resp = evo_bull.invoke(bull_prompt)
            bull_arg = resp.content if hasattr(resp, "content") else str(resp)
        except Exception as exc:
            logger.warning("Stock bull debate failed for %s: %s", ticker, exc)
            bull_arg = "（多头未能构建有效论点）"

        # Bear argument — always sees bull's current-round argument
        bear_prompt = _STOCK_BEAR_PROMPT.format(
            ticker=ticker, stock_name=stock_name, industry=industry,
            fund_score=round(stock_result.fundamentals.score, 1),
            tech_score=round(stock_result.technical.score, 1),
            event_score=round(stock_result.event_match.score, 1),
            composite_score=round(stock_result.composite, 1),
            events_summary=events_summary,
        )
        bear_prompt += f"\n\n## 多头本轮论点\n{bull_arg}\n"
        if round_num > 0 and debate_history:
            bear_prompt += f"## 前轮辩论\n{debate_history}\n"
        bear_prompt += "请针对多头的论点进行逐点反驳。"

        try:
            resp = evo_bear.invoke(bear_prompt)
            bear_arg = resp.content if hasattr(resp, "content") else str(resp)
        except Exception as exc:
            logger.warning("Stock bear debate failed for %s: %s", ticker, exc)
            bear_arg = "（空头未能构建有效论点）"

        debate_history += f"\n--- 第 {round_num + 1} 轮 ---\n多头：{bull_arg}\n空头：{bear_arg}\n"

    # Judge
    judge_prompt = _STOCK_JUDGE_PROMPT.format(
        ticker=ticker, stock_name=stock_name,
        bull_argument=bull_arg, bear_argument=bear_arg,
        debate_history=debate_history,
    )

    # Try structured output
    try:
        structured_llm = evo_judge.with_structured_output(StockDebateResult)
        result = structured_llm.invoke(judge_prompt)
        if isinstance(result, StockDebateResult):
            return result
        return StockDebateResult.model_validate(result)
    except Exception:
        pass

    # Fallback: free-text
    try:
        resp = evo_judge.invoke(judge_prompt)
        text = resp.content if hasattr(resp, "content") else str(resp)
        return _parse_stock_verdict(text)
    except Exception as exc:
        logger.warning("Stock judge failed for %s: %s", ticker, exc)
        return StockDebateResult(
            debate_score=50.0,
            bull_bear_bias="neutral",
            bull_bear_summary=f"辩论失败: {exc}",
        )


def debate_batch_stocks(
    stock_results: list[StockScoreResult],
    events: list[dict],
    llm_quick: Any,
    llm_deep: Any,
) -> list[StockDebateResult]:
    """Run debates on multiple stocks in parallel.

    Args:
        stock_results: List of scored stocks from score_all_candidates.
        events: Related events.
        llm_quick: LLM for bull/bear.
        llm_deep: LLM for judge.

    Returns:
        List of StockDebateResult, one per stock.
    """
    if not stock_results:
        return []

    results: list[StockDebateResult | None] = [None] * len(stock_results)

    with ThreadPoolExecutor(max_workers=DEBATE_MAX_WORKERS) as executor:
        future_to_idx = {}
        for idx, stock in enumerate(stock_results):
            future = executor.submit(
                debate_stock, stock, events, llm_quick, llm_deep,
            )
            future_to_idx[future] = idx

        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                logger.error("Stock debate failed for item %d: %s", idx, exc)
                results[idx] = StockDebateResult(
                    debate_score=50.0,
                    bull_bear_bias="neutral",
                    bull_bear_summary=f"辩论失败: {exc}",
                )

    return [r if r is not None else StockDebateResult() for r in results]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_stock_verdict(text: str) -> StockDebateResult:
    """Parse stock debate verdict from free-text."""
    import re

    json_match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group())
            return StockDebateResult.model_validate(data)
        except Exception:
            pass

    # Extract bias
    bias = "neutral"
    bias_match = re.search(
        r"(?:bull_bear_bias|多空倾向)[：:\s]*(bullish|bearish|neutral|看多|看空|中性)",
        text, re.IGNORECASE,
    )
    if bias_match:
        val = bias_match.group(1).lower()
        if val in ("bullish", "看多"):
            bias = "bullish"
        elif val in ("bearish", "看空"):
            bias = "bearish"

    # Extract score
    score = 50.0
    score_match = re.search(r"(?:debate_score|辩论得分)[：:\s]*(\d+\.?\d*)", text, re.IGNORECASE)
    if score_match:
        score = max(0.0, min(100.0, float(score_match.group(1))))

    return StockDebateResult(
        debate_score=score,
        bull_bear_bias=bias,
        bull_bear_summary=text[:200] if text else "无法解析",
    )
