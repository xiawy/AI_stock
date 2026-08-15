"""O1 并行分析师层的回归测试。

覆盖点：
1. 并行图能编译、能完整跑通（分析师 → 质量门 → 辩论 → 决策）。
2. 分析师真的在并行跑（时间区间重叠），而不是退化为串行。
3. 私有消息通道隔离：一个分析师看不到另一个分析师的工具循环。
4. 主通道 messages 只汇总各分析师的最终报告。
5. Quality Gate 是 fan-in barrier：所有报告就绪后才执行（B1 动态阈值生效）。
"""

import time
import types
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import ai_stock.graph.setup as graph_setup_mod
from ai_stock.agents.quality_gate import create_quality_gate
from ai_stock.graph.conditional_logic import ConditionalLogic
from ai_stock.graph.propagation import Propagator
from ai_stock.graph.setup import GraphSetup

# 超过 MIN_REPORT_LENGTH=200 且带表格标记，硬检查拿 A 级
GOOD_REPORT = ("| 日期 | 涨跌 |\n|---|---|\n| 2026-08-14 | +1.2% |\n" + "分析内容。" * 80)


class FakeToolNode:
    """Duck-typed ToolNode：回一条匹配 tool_call_id 的 ToolMessage。"""

    def invoke(self, state, config=None, **kwargs):
        last = state["messages"][-1]
        return {
            "messages": [
                ToolMessage(content="fake tool data", tool_call_id=tc["id"])
                for tc in (last.tool_calls or [])
            ]
        }

    # add_node 要求 Runnable/callable；串行分支直接把 tool_nodes 值注册为节点
    def __call__(self, state, config=None, **kwargs):
        return self.invoke(state, config)


class FakeGateLLM:
    def invoke(self, prompt):
        return SimpleNamespace(content="## LLM 复审：整体可用（fake）")


def make_fake_analyst(report_key, intervals, delay=0.3):
    """两轮节点：首轮发 tool_calls（记录耗时区间），次轮交最终报告。"""

    def node(state):
        msgs = state["messages"]
        if msgs and isinstance(msgs[-1], ToolMessage):
            return {
                "messages": [AIMessage(content=GOOD_REPORT)],
                report_key: GOOD_REPORT,
            }
        t0 = time.time()
        time.sleep(delay)
        intervals[report_key] = (t0, time.time())
        return {
            "messages": [
                AIMessage(
                    content="fetching data",
                    tool_calls=[
                        {
                            "name": "get_stock_data",
                            "args": {"symbol": "600519"},
                            "id": f"call_{report_key}",
                        }
                    ],
                )
            ]
        }

    return node


def make_fake_debator(name, state_key, response_prefix):
    def node(state):
        debate = dict(state.get(state_key) or {})
        debate["count"] = debate.get("count", 0) + 1
        debate["current_response"] = response_prefix
        debate["latest_speaker"] = name
        return {"messages": [AIMessage(content=f"{name} response")], state_key: debate}

    return node


@pytest.fixture
def patched_factories(monkeypatch):
    intervals = {}

    analyst_factory = {
        "market": graph_setup_mod.create_market_analyst,
        "social": graph_setup_mod.create_social_media_analyst,
        "news": graph_setup_mod.create_news_analyst,
        "fundamentals": graph_setup_mod.create_fundamentals_analyst,
        "policy": graph_setup_mod.create_policy_analyst,
        "hot_money": graph_setup_mod.create_hot_money_tracker,
    }
    report_keys = {
        "market": "market_report",
        "social": "sentiment_report",
        "news": "news_report",
        "fundamentals": "fundamentals_report",
        "policy": "policy_report",
        "hot_money": "hot_money_report",
    }
    for role, factory in analyst_factory.items():
        monkeypatch.setattr(
            graph_setup_mod,
            factory.__name__,
            lambda llm, _role=role: make_fake_analyst(
                report_keys[_role], intervals
            ),
        )

    def fake_bull(state):
        debate = dict(state.get("investment_debate_state") or {})
        debate["count"] = debate.get("count", 0) + 1
        debate["current_response"] = "Bull says buy"
        return {
            "messages": [AIMessage(content="bull")],
            "investment_debate_state": debate,
        }

    def fake_bear(state):
        debate = dict(state.get("investment_debate_state") or {})
        debate["count"] = debate.get("count", 0) + 1
        debate["current_response"] = "Bear says sell"
        return {
            "messages": [AIMessage(content="bear")],
            "investment_debate_state": debate,
        }

    monkeypatch.setattr(graph_setup_mod, "create_bull_researcher", lambda llm: fake_bull)
    monkeypatch.setattr(graph_setup_mod, "create_bear_researcher", lambda llm: fake_bear)
    monkeypatch.setattr(
        graph_setup_mod,
        "create_research_manager",
        lambda llm: (
            lambda state: {
                "messages": [AIMessage(content="plan")],
                "investment_plan": "hold",
            }
        ),
    )
    monkeypatch.setattr(
        graph_setup_mod,
        "create_trader",
        lambda llm: (
            lambda state: {
                "messages": [AIMessage(content="trade")],
                "trader_investment_plan": "BUY 100",
            }
        ),
    )
    for factory_name, speaker in (
        ("create_aggressive_debator", "Aggressive Analyst"),
        ("create_neutral_debator", "Neutral Analyst"),
        ("create_conservative_debator", "Conservative Analyst"),
    ):
        monkeypatch.setattr(
            graph_setup_mod,
            factory_name,
            lambda llm, _s=speaker: make_fake_debator(
                _s, "risk_debate_state", _s
            ),
        )
    monkeypatch.setattr(
        graph_setup_mod,
        "create_portfolio_manager",
        lambda llm: (
            lambda state: {
                "messages": [AIMessage(content="done")],
                "final_trade_decision": "BUY",
            }
        ),
    )
    return intervals


def _build_graph(selected, parallel=True):
    gate_llm = FakeGateLLM()
    return GraphSetup(
        quick_thinking_llm=gate_llm,
        deep_thinking_llm=gate_llm,
        tool_nodes={role: FakeToolNode() for role in selected},
        conditional_logic=ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1),
        config={"evolution_enabled": False, "parallel_analysts": parallel},
    ).setup_graph(selected)


def test_parallel_graph_runs_end_to_end_and_reports_isolated(patched_factories):
    selected = ["market", "social", "news"]
    graph = _build_graph(selected).compile()
    init_state = Propagator().create_initial_state("600519", "2026-08-14")

    final_state = graph.invoke(init_state, {"recursion_limit": 100})

    # 1. 每个选中分析师都产出报告
    for key in ("market_report", "sentiment_report", "news_report"):
        assert final_state.get(key), f"{key} 缺失"

    # 2. 最终决策存在 → 整条链（gate → debate → trader → risk → PM）走通
    assert final_state.get("final_trade_decision") == "BUY"

    # 3. 私有通道隔离：market 通道里不能出现 social 的 tool_call id
    market_msgs = final_state.get("market_messages", [])
    market_call_ids = {
        tc["id"] for m in market_msgs for tc in (getattr(m, "tool_calls", None) or [])
    }
    assert market_call_ids == {"call_market_report"}, market_call_ids
    social_msgs = final_state.get("social_messages", [])
    social_call_ids = {
        tc["id"] for m in social_msgs for tc in (getattr(m, "tool_calls", None) or [])
    }
    assert social_call_ids == {"call_sentiment_report"}, social_call_ids

    # 4. 主通道：各分析师最终报告 + 初始 human 消息都可见，
    #    但主通道不应包含任何工具调用消息（那些只在私有通道里）
    main_msgs = final_state.get("messages", [])
    main_tool_call_ids = {
        tc["id"] for m in main_msgs for tc in (getattr(m, "tool_calls", None) or [])
    }
    assert main_tool_call_ids == set(), main_tool_call_ids
    assert sum(1 for m in main_msgs if isinstance(m, AIMessage)) >= len(selected)

    # 5. Quality Gate 产物存在且 LLM 复审（fail_count=0 < 阈值）被执行
    summary = final_state.get("data_quality_summary", "")
    assert "数据质量门控结果" in summary
    assert "fake" in summary  # FakeGateLLM 的输出被采纳


def test_analysts_actually_run_in_parallel(patched_factories):
    selected = ["market", "social", "news"]
    graph = _build_graph(selected).compile()
    init_state = Propagator().create_initial_state("600519", "2026-08-14")

    graph.invoke(init_state, {"recursion_limit": 100})

    intervals = patched_factories
    assert len(intervals) == 3, intervals
    # 任意两个分析师的首轮耗时区间必须有重叠：串行图里三者首尾相接不可能重叠
    keys = sorted(intervals)
    for i in range(len(keys) - 1):
        a0, a1 = intervals[keys[i]]
        b0, b1 = intervals[keys[i + 1]]
        overlap = min(a1, b1) - max(a0, b0)
        assert overlap > 0.1, f"{keys[i]} 与 {keys[i+1]} 没有并行重叠: {intervals}"


def test_sequential_fallback_still_works(patched_factories):
    """parallel_analysts=False 时回退到串行链，行为照旧。"""
    selected = ["market", "news"]
    graph = _build_graph(selected, parallel=False).compile()
    init_state = Propagator().create_initial_state("600519", "2026-08-14")

    final_state = graph.invoke(init_state, {"recursion_limit": 100})

    assert final_state.get("market_report")
    assert final_state.get("news_report")
    assert final_state.get("final_trade_decision") == "BUY"
    # 串行模式走 Msg Clear 节点：主通道里分析师消息会被清理，
    # 但报告字段正常产出（此为旧行为的契约）


def test_quality_gate_threshold_scales_with_active_analysts():
    """B1：只选 2 个分析师时，fail_count=1（旧阈值 4 会放行）仍要触发 LLM 复审。"""
    from unittest.mock import MagicMock

    calls = []

    def llm_invoke(prompt):
        calls.append(prompt)
        return SimpleNamespace(content="review ok")

    llm = MagicMock()
    llm.invoke = llm_invoke

    gate = create_quality_gate(llm, active_analysts=["market", "news"])
    state = {
        "trade_date": "2026-08-14",
        "company_of_interest": "600519",
        # market 报告为空 → F；news 报告合格 → A。fail_count=1 < max(2, 2)=2 → 复审
        "market_report": "",
        "news_report": GOOD_REPORT,
    }
    out = gate(state)
    assert "数据质量门控结果" in out["data_quality_summary"]
    assert calls, "fail_count=1 且选中 2 人时必须触发 LLM 复审（B1）"
    prompt = calls[0]
    assert "2 位分析师" in prompt
    assert "技术分析师" in prompt and "新闻分析师" in prompt
    assert "情绪分析师" not in prompt  # 未选中的分析师不进 prompt
