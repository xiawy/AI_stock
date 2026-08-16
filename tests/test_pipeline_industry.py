"""Tests for the industry board (行业榜) pipeline extension.

Covers the "宏观情绪 → 中观行业 → 微观个股" data flow:
- AgentScoreResult primary/secondary industry fields (llm_judge)
- _merge_industry_labels cross-agent voting + priority fallback (scoring)
- calculate_industry_heatmap weighted aggregation + resonance/rating (ranking)
- generate_candidate_pool industry-leader P0 injection (candidate_pool)
- Propagator.create_initial_state industry context fields (诊股联动)
"""

import pytest
from unittest.mock import MagicMock

from ai_stock.pipeline.llm_judge import AgentScoreResult
from ai_stock.pipeline.scoring import _merge_industry_labels
from ai_stock.pipeline.ranking import calculate_industry_heatmap
from ai_stock.pipeline.candidate_pool import generate_candidate_pool
from ai_stock.graph.propagation import Propagator


def _agent(primary="", secondary="", score=7.0, industries=None):
    return AgentScoreResult(
        score=score,
        reasoning="test",
        primary_industry=primary,
        secondary_industry=secondary,
        industries=industries or [],
    )


def _news(score, bias="bullish", primary="", secondary=""):
    return {
        "composite_score": score,
        "bull_bear_bias": bias,
        "primary_industry": primary,
        "secondary_industry": secondary,
    }


@pytest.mark.unit
class TestAgentScoreResultIndustryFields:
    def test_defaults_empty(self):
        r = AgentScoreResult(score=7.5, reasoning="test")
        assert r.primary_industry == ""
        assert r.secondary_industry == ""

    def test_explicit_values(self):
        r = AgentScoreResult(
            score=8.0,
            reasoning="test",
            primary_industry="电子",
            secondary_industry="计算机",
        )
        assert r.primary_industry == "电子"
        assert r.secondary_industry == "计算机"


@pytest.mark.unit
class TestMergeIndustryLabels:
    def test_majority_vote_wins(self):
        # policy + news both vote 电子 as primary
        results = (
            _agent("电子", "计算机"),
            _agent("电子", "医药生物"),
            _agent("银行"),
            _agent(),
        )
        primary, secondary = _merge_industry_labels(*results)
        assert primary == "电子"
        # secondary votes split 1v1 → priority fallback lands on policy's 计算机
        assert secondary == "计算机"

    def test_priority_fallback_when_no_majority(self):
        # policy vs news disagree — policy outranks news
        results = (
            _agent("银行"),
            _agent("电子"),
            _agent(),
            _agent(),
        )
        primary, _ = _merge_industry_labels(*results)
        assert primary == "银行"

    def test_priority_order_capital_beats_news(self):
        results = (
            _agent(),
            _agent("电子"),
            _agent("证券"),
            _agent(),
        )
        primary, _ = _merge_industry_labels(*results)
        assert primary == "证券"

    def test_secondary_excludes_primary(self):
        results = (
            _agent("电子", "电子"),
            _agent("电子", "电子"),
            _agent(),
            _agent(),
        )
        primary, secondary = _merge_industry_labels(*results)
        assert primary == "电子"
        assert secondary == ""

    def test_all_empty(self):
        assert _merge_industry_labels(None, None, None, None) == ("", "")

    def test_single_agent_fallback(self):
        results = (_agent("电子", "计算机"), None, None, None)
        primary, secondary = _merge_industry_labels(*results)
        assert primary == "电子"
        assert secondary == "计算机"

    def test_industries_fallback_majority(self):
        # Batch LLM output often leaves primary/secondary empty while still
        # filling industries — industries[0]/[1] act as implicit votes.
        results = (
            _agent(industries=["半导体", "算力"]),
            _agent(industries=["半导体", "光模块"]),
            _agent(industries=["半导体"]),
            _agent(),
        )
        primary, secondary = _merge_industry_labels(*results)
        assert primary == "半导体"  # 3 implicit votes → majority
        # secondary votes split (算力 1 / 光模块 1) → priority fallback → policy's 算力
        assert secondary == "算力"

    def test_industries_fallback_single_opinion(self):
        results = (
            _agent(industries=["算力"]),
            _agent(),
            _agent(),
            _agent(),
        )
        primary, secondary = _merge_industry_labels(*results)
        assert primary == "算力"  # priority fallback on implicit vote
        assert secondary == ""

    def test_industries_ignored_when_labels_present(self):
        # Explicit labels win; industries must not inject extra votes.
        results = (
            _agent("银行", industries=["半导体"]),
            _agent("银行", industries=["半导体"]),
            _agent(industries=["半导体"]),
            _agent(),
        )
        primary, secondary = _merge_industry_labels(*results)
        assert primary == "银行"

    def test_industries_all_empty_still_empty(self):
        results = (_agent(industries=[]), _agent(), _agent(), _agent())
        assert _merge_industry_labels(*results) == ("", "")


@pytest.mark.unit
class TestCalculateIndustryHeatmap:
    def test_empty_input(self):
        assert calculate_industry_heatmap([]) == []

    def test_no_industry_labels(self):
        assert calculate_industry_heatmap([_news(8.0)]) == []

    def test_weighted_aggregation(self):
        # heat = Σ composite × weight(primary 1.0 / secondary 0.5) × bias factor
        news = [
            _news(8.0, "bullish", primary="电子", secondary="计算机"),
            _news(10.0, "neutral", primary="计算机"),
            _news(6.0, "bearish", primary="银行"),
        ]
        result = calculate_industry_heatmap(news)
        by_industry = {r["industry"]: r for r in result}
        assert by_industry["电子"]["heat_score"] == pytest.approx(8.0)
        assert by_industry["计算机"]["heat_score"] == pytest.approx(4.0 + 6.0)
        assert by_industry["银行"]["heat_score"] == pytest.approx(1.2)

    def test_sorted_and_ranked(self):
        news = [
            _news(6.0, primary="银行"),
            _news(9.0, primary="电子"),
        ]
        result = calculate_industry_heatmap(news)
        assert [r["industry"] for r in result] == ["电子", "银行"]
        assert [r["rank"] for r in result] == [1, 2]

    def test_top_n_truncation(self):
        news = [_news(5.0 + i, primary=f"行业{i}") for i in range(5)]
        result = calculate_industry_heatmap(news, top_n=3)
        assert len(result) == 3
        assert result[0]["industry"] == "行业4"  # highest heat first

    def test_no_flows_degrades_to_none(self):
        result = calculate_industry_heatmap([_news(8.0, primary="电子")])
        assert result[0]["resonance"] == "none"
        assert result[0]["fund_flow_net"] is None
        assert result[0]["rating"] == "B"  # hot industry without flow data

    def test_strong_resonance_rating_a(self):
        news = [
            _news(9.0, primary="电子"),
            _news(2.0, primary="银行"),
        ]
        flows = [
            {
                "name": "电子", "code": "BK1033", "main_net_inflow": 5e9,
                "change_pct": 2.5, "top_stock_name": "中兴通讯",
                "top_stock_code": "000063",
            },
            {"name": "银行", "code": "BK0475", "main_net_inflow": -1e9, "change_pct": -0.3},
        ]
        result = calculate_industry_heatmap(news, flows)
        by = {r["industry"]: r for r in result}
        # 电子: hot (9.0 >= 4.5) + meaningful inflow → strong / A
        assert by["电子"]["resonance"] == "strong"
        assert by["电子"]["rating"] == "A"
        assert by["电子"]["industry_code"] == "BK1033"
        assert by["电子"]["top_stock_code"] == "000063"
        # 银行: not hot + outflow → none / C
        assert by["银行"]["resonance"] == "none"
        assert by["银行"]["rating"] == "C"

    def test_divergence_when_hot_but_outflow(self):
        news = [
            _news(9.0, primary="电子"),
            _news(2.0, primary="银行"),
        ]
        flows = [
            {"name": "电子", "main_net_inflow": -5e9},
            {"name": "银行", "main_net_inflow": 5e9},
        ]
        result = calculate_industry_heatmap(news, flows)
        by = {r["industry"]: r for r in result}
        # 电子: hot + outflow → divergence warning; heat alone still rates B
        assert by["电子"]["resonance"] == "divergence"
        assert by["电子"]["rating"] == "B"
        # 银行: not hot + meaningful inflow → 资金潜伏; heat_norm 0.22 < 0.3 → C
        assert by["银行"]["resonance"] == "quiet"
        assert by["银行"]["rating"] == "C"

    def test_substring_flow_matching(self):
        # LLM outputs 申万-style "电子", the Eastmoney board is "电子元件"
        news = [_news(8.0, primary="电子")]
        flows = [
            {"name": "电子元件", "code": "BK100", "main_net_inflow": 3e9, "change_pct": 1.2},
        ]
        result = calculate_industry_heatmap(news, flows)
        assert result[0]["industry_code"] == "BK100"
        assert result[0]["fund_flow_net"] == pytest.approx(3e9)
        assert result[0]["resonance"] == "strong"


@pytest.mark.unit
class TestCandidatePoolIndustryLeaders:
    def test_leaders_injected_at_p0(self):
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content="[]")
        events = [
            {
                "title": "芯片出口管制放松",
                "industries": ["电子"],
                "top_stocks": [{"code": "300750", "name": "宁德时代", "elasticity": 0.9}],
            },
        ]
        limit_up = [{"code": "300750", "name": "宁德时代", "reason_tags": ["电力设备"]}]
        leaders = [{"code": "300750", "name": "宁德时代", "industry": "电力设备", "rank": 1}]

        pool = generate_candidate_pool(events, limit_up, llm, industry_leaders=leaders)

        assert pool
        top = pool[0]
        assert top["code"] == "300750"
        assert top["source_tier"] == "P0"
        # Both the industry board and the event reference the stock
        assert top["event_match_count"] == 2
        assert "行业榜Top1·电力设备" in top["matched_events"]
        assert "芯片出口管制放松" in top["matched_events"]
        assert "电力设备" in top["matched_industries"]

    def test_leader_only_without_event_hit(self):
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content="[]")
        leaders = [{"code": "600519", "name": "贵州茅台", "industry": "食品饮料", "rank": 2}]

        pool = generate_candidate_pool([], [], llm, industry_leaders=leaders)

        assert pool and pool[0]["code"] == "600519"
        assert pool[0]["source_tier"] == "P0"
        assert pool[0]["matched_events"] == ["行业榜Top2·食品饮料"]

    def test_leaders_optional(self):
        pool = generate_candidate_pool([], [], MagicMock(), industry_leaders=None)
        assert pool == []


@pytest.mark.unit
class TestInitialStateIndustryContext:
    def test_fields_initialized(self):
        state = Propagator().create_initial_state(
            "300750",
            "2026-08-16",
            industry_heatmap="1. 电子 热度9.0 主力净流入+5.2亿 评级A",
            hot_sector_stocks="宁德时代(电力设备)、中兴通讯(电子)",
        )
        assert state["industry_heatmap"].startswith("1. 电子")
        assert "宁德时代" in state["hot_sector_stocks"]

    def test_fields_default_empty(self):
        state = Propagator().create_initial_state("300750", "2026-08-16")
        assert state["industry_heatmap"] == ""
        assert state["hot_sector_stocks"] == ""
