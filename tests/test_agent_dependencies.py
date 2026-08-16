"""阶段间数据依赖契约测试（半并行架构依赖表）。

- 过渡 质量门控：6 份研报 + 工具调用台账（私有通道统计）。
- 阶段三 RM：辩论记录 + 6 份研报（通过 State）+ 质量门控摘要。
- 阶段四 Trader：仅 RM 投资计划，不直读研报。
- 阶段五 风险辩手：仅 Trader 交易方案 + 阶段内辩论记录，不直读研报。
- 阶段六 PM：Trader 交易方案 + 风险辩论记录，不直读 RM 投资计划。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import ToolMessage

from ai_stock.agents.managers.portfolio_manager import create_portfolio_manager
from ai_stock.agents.managers.research_manager import create_research_manager
from ai_stock.agents.quality_gate import create_quality_gate
from ai_stock.agents.risk_mgmt.aggressive_debator import create_aggressive_debator
from ai_stock.agents.risk_mgmt.conservative_debator import create_conservative_debator
from ai_stock.agents.risk_mgmt.neutral_debator import create_neutral_debator
from ai_stock.agents.schemas import (
    PortfolioDecision,
    PortfolioRating,
    ResearchPlan,
    TraderAction,
    TraderProposal,
)
from ai_stock.agents.trader.trader import create_trader

# 研报内容标记：出现在某 agent 的 prompt 里即证明它直读了研报
REPORT_MARKERS = {
    "market_report": "MARKER-MARKET-REPORT",
    "sentiment_report": "MARKER-SENTIMENT-REPORT",
    "news_report": "MARKER-NEWS-REPORT",
    "fundamentals_report": "MARKER-FUNDAMENTALS-REPORT",
    "policy_report": "MARKER-POLICY-REPORT",
    "hot_money_report": "MARKER-HOTMONEY-REPORT",
}
PLAN_MARKER = "MARKER-INVESTMENT-PLAN"
TRADER_MARKER = "MARKER-TRADER-PLAN"
DEBATE_MARKER = "MARKER-DEBATE-HISTORY"
RISK_HISTORY_MARKER = "MARKER-RISK-DEBATE-HISTORY"
DQS_MARKER = "MARKER-DATA-QUALITY"


def _capture_structured(schema_instance):
    """MagicMock LLM：with_structured_output 路径捕获 prompt 并返回合法实例。"""
    captured = {}
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or schema_instance
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm, captured


def _prompt_text(prompt):
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        return "\n".join(
            str(m["content"] if isinstance(m, dict) else getattr(m, "content", ""))
            for m in prompt
        )
    return str(prompt)


def _base_state():
    state = {
        "company_of_interest": "NVDA",
        "investment_plan": PLAN_MARKER,
        "trader_investment_plan": TRADER_MARKER,
        "data_quality_summary": DQS_MARKER,
        "investment_debate_state": {
            "history": DEBATE_MARKER,
            "bull_history": "Bull...",
            "bear_history": "Bear...",
            "current_response": "",
            "judge_decision": "",
            "count": 2,
        },
        "risk_debate_state": {
            "history": RISK_HISTORY_MARKER,
            "aggressive_history": "A...",
            "conservative_history": "C...",
            "neutral_history": "N...",
            "latest_speaker": "Aggressive",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "count": 3,
        },
    }
    state.update(REPORT_MARKERS)
    return state


class TestResearchManagerDependency:
    def test_prompt_contains_reports_debate_and_quality_summary(self):
        """阶段三：RM 的裁决依据 = 辩论记录 + 6 份研报（State）+ 质量摘要。"""
        llm, captured = _capture_structured(ResearchPlan(
            recommendation=PortfolioRating.HOLD,
            rationale="balanced",
            strategic_actions="hold",
        ))
        create_research_manager(llm)(_base_state())
        text = _prompt_text(captured["prompt"])
        for marker in REPORT_MARKERS.values():
            assert marker in text, f"RM prompt 缺少研报 {marker}"
        assert DEBATE_MARKER in text
        assert DQS_MARKER in text


class TestTraderDependency:
    def test_prompt_contains_only_investment_plan(self):
        """阶段四：Trader 只依赖 RM 投资计划，不直读研报。"""
        llm, captured = _capture_structured(TraderProposal(
            action=TraderAction.HOLD, reasoning="r"
        ))
        create_trader(llm)(_base_state())
        text = _prompt_text(captured["prompt"])
        assert PLAN_MARKER in text
        for marker in REPORT_MARKERS.values():
            assert marker not in text, f"Trader 不应直读研报：{marker}"


@pytest.mark.parametrize(
    "factory",
    [create_aggressive_debator, create_conservative_debator, create_neutral_debator],
    ids=["aggressive", "conservative", "neutral"],
)
class TestRiskDebatorDependency:
    def test_prompt_contains_only_trader_plan_and_debate(self, factory):
        """阶段五：风险辩手只依赖 Trader 交易方案 + 本阶段辩论记录。"""
        llm = MagicMock()
        llm.invoke.return_value = SimpleNamespace(content="argument")
        factory(llm)(_base_state())
        text = llm.invoke.call_args[0][0]
        assert TRADER_MARKER in text
        assert RISK_HISTORY_MARKER in text
        for marker in REPORT_MARKERS.values():
            assert marker not in text, f"风险辩手不应直读研报：{marker}"


class TestPortfolioManagerDependency:
    def test_prompt_contains_trader_plan_and_risk_debate_only(self):
        """阶段六：PM 只依赖交易方案 + 风险辩论，不直读 RM 投资计划。"""
        llm, captured = _capture_structured(PortfolioDecision(
            rating=PortfolioRating.HOLD,
            executive_summary="hold",
            investment_thesis="balanced",
        ))
        create_portfolio_manager(llm)(_base_state())
        text = _prompt_text(captured["prompt"])
        assert TRADER_MARKER in text
        assert RISK_HISTORY_MARKER in text
        assert PLAN_MARKER not in text, "PM 不应直读 RM 投资计划"


class TestQualityGateLedger:
    def test_summary_includes_tool_ledger_from_private_channels(self):
        """过渡：门控摘要包含各分析师私有通道的工具调用台账。"""
        llm = MagicMock()
        llm.invoke.return_value = SimpleNamespace(content="review ok")
        gate = create_quality_gate(llm, active_analysts=["market", "news"])
        state = _base_state()
        state["trade_date"] = "2026-08-14"
        state["market_messages"] = [
            ToolMessage(content="d", tool_call_id="c1", name="get_stock_data"),
            ToolMessage(content="d", tool_call_id="c2", name="get_stock_data"),
            ToolMessage(content="d", tool_call_id="c3", name="get_stock_news"),
        ]
        state["news_messages"] = [
            ToolMessage(content="d", tool_call_id="c4", name="get_stock_news"),
        ]
        summary = gate(state)["data_quality_summary"]
        assert "工具调用台账" in summary
        assert "技术分析师: 3 次调用 (get_stock_data×2, get_stock_news×1)" in summary
        assert "新闻分析师: 1 次调用 (get_stock_news×1)" in summary

    def test_ledger_absent_without_private_channels(self):
        """串行模式没有私有通道时不出台账段，摘要其余部分照常。"""
        llm = MagicMock()
        gate = create_quality_gate(llm, active_analysts=["market", "news"])
        state = _base_state()
        state["trade_date"] = "2026-08-14"
        summary = gate(state)["data_quality_summary"]
        assert "工具调用台账" not in summary
        assert "数据质量门控结果" in summary
