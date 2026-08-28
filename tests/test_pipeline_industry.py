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

    def test_alias_resolves_fine_concept_board(self):
        # LLM outputs "封测"; alias resolves to concept board "先进封装"
        news = [_news(8.0, primary="封测")]
        flows = [
            {"name": "先进封装", "code": "BK1101", "main_net_inflow": 4e9,
             "change_pct": 3.1, "board_level": "concept"},
        ]
        result = calculate_industry_heatmap(news, flows)
        assert result[0]["board_name"] == "先进封装"
        assert result[0]["industry_code"] == "BK1101"
        assert result[0]["industry_level"] == "concept"

    def test_mlcc_exact_concept_match_over_coarse_industry(self):
        # "MLCC" must hit the fine concept board, not the coarse 电子元件 industry
        news = [_news(7.5, primary="MLCC")]
        flows = [
            {"name": "MLCC", "code": "BK0890", "main_net_inflow": 2e9,
             "change_pct": 1.5, "board_level": "concept"},
            {"name": "电子元件", "code": "BK1334", "main_net_inflow": 1e9,
             "change_pct": 0.5, "board_level": "industry"},
        ]
        result = calculate_industry_heatmap(news, flows)
        assert result[0]["industry_code"] == "BK0890"
        assert result[0]["industry_level"] == "concept"

    def test_suffix_normalized_match(self):
        # LLM "被动元件" matches board "被动元件概念" via suffix-stripping
        news = [_news(7.0, primary="被动元件")]
        flows = [
            {"name": "被动元件概念", "code": "BK0976", "main_net_inflow": 1e9,
             "change_pct": 2.0, "board_level": "concept"},
        ]
        result = calculate_industry_heatmap(news, flows)
        assert result[0]["industry_code"] == "BK0976"

    def test_unmatched_industry_has_empty_level(self):
        # No flow match → industry_level empty, code empty
        news = [_news(7.0, primary="未知题材")]
        result = calculate_industry_heatmap(news, [])
        assert result[0]["board_name"] == ""
        assert result[0]["industry_code"] == ""
        assert result[0]["industry_level"] == ""

    def test_new_theme_auto_resolves_without_alias(self):
        # A brand-new theme (机器人) resolves via substring — no alias entry
        # needed, so the alias table never has to grow as 题材 evolve.
        news = [_news(8.0, primary="机器人")]
        flows = [
            {"name": "机器人概念", "code": "BK1108", "main_net_inflow": 2e9,
             "change_pct": 2.0, "board_level": "concept"},
        ]
        result = calculate_industry_heatmap(news, flows)
        assert result[0]["industry_code"] == "BK1108"
        assert result[0]["industry_level"] == "concept"

    def test_fuzzy_fallback_bridges_typo(self):
        # difflib fallback bridges a dropped-character typo 存芯片→存储芯片,
        # which exact/normalize/substring all miss (储 is skipped in the label).
        news = [_news(7.5, primary="存芯片")]
        flows = [
            {"name": "存储芯片", "code": "BK1137", "main_net_inflow": 1e9,
             "change_pct": 1.5, "board_level": "concept"},
        ]
        result = calculate_industry_heatmap(news, flows)
        assert result[0]["industry_code"] == "BK1137"
        assert result[0]["industry_level"] == "concept"


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
        from ai_stock.graph.propagation import Propagator

        state = Propagator().create_initial_state(
            "300750",
            "2026-08-16",
            industry_heatmap="1. 电子 热度9.0 主力净流入+5.2亿 评级A",
            hot_sector_stocks="宁德时代(电力设备)、中兴通讯(电子)",
        )
        assert state["industry_heatmap"].startswith("1. 电子")
        assert "宁德时代" in state["hot_sector_stocks"]

    def test_fields_default_empty(self):
        from ai_stock.graph.propagation import Propagator

        state = Propagator().create_initial_state("300750", "2026-08-16")
        assert state["industry_heatmap"] == ""
        assert state["hot_sector_stocks"] == ""


@pytest.mark.unit
class TestIndustryHeatmapTopStockPct:
    def test_top_stock_pct_propagated(self):
        news = [_news(8.0, primary="电子")]
        flows = [{
            "name": "电子", "code": "BK1033", "main_net_inflow": 5e9,
            "change_pct": 2.0, "top_stock_name": "中兴通讯",
            "top_stock_code": "000063", "top_stock_pct": 5.1,
        }]
        result = calculate_industry_heatmap(news, flows)
        assert result[0]["top_stock_pct"] == 5.1
        assert result[0]["top_stock_code"] == "000063"


@pytest.mark.unit
class TestIndustryLeaderComposite:
    """龙头口径 = 领涨 + 板块最相关 + 弹性最大（不再按市值）。"""

    @staticmethod
    def _stock(code, name, change, turnover=1.0, vol_ratio=1.0, inflow=0.0):
        return {
            "code": code, "name": name, "change_pct": change,
            "turnover_rate": turnover, "volume_ratio": vol_ratio,
            "market_cap": 0.0, "main_net_inflow": inflow,
        }

    def test_gain_dominates(self):
        from ai_stock.dataflows.pipeline_data import _rank_board_leaders
        stocks = [
            self._stock("600001", "低涨幅", 6.0),
            self._stock("600002", "高涨幅", 8.0),
        ]
        leaders = _rank_board_leaders(stocks, 2.0, "", 5)
        assert [s["code"] for s in leaders] == ["600002", "600001"]
        assert leaders[0]["leader_label"] == "龙头"

    def test_board_top_gainer_labeled_lizhang(self):
        from ai_stock.dataflows.pipeline_data import _rank_board_leaders
        stocks = [
            self._stock("600001", "普通股", 5.0),
            self._stock("600002", "官方领涨", 5.5),
        ]
        leaders = _rank_board_leaders(stocks, 1.0, "600002", 5)
        assert leaders[0]["code"] == "600002"
        assert leaders[0]["leader_label"] == "领涨"
        assert leaders[1]["leader_label"] == "龙头"

    def test_limit_up_relevance_boost(self):
        from ai_stock.dataflows.pipeline_data import _rank_board_leaders
        # 9.8% 涨停（相关加成 +3）压过 9.5% 未涨停但换手活跃的股票
        stocks = [
            self._stock("600001", "未涨停高换手", 9.5, turnover=20.0, vol_ratio=3.0),
            self._stock("600002", "涨停", 9.8, turnover=2.0, vol_ratio=1.0),
        ]
        leaders = _rank_board_leaders(stocks, 3.0, "", 5)
        assert leaders[0]["code"] == "600002"

    def test_elasticity_volume_ratio_tiebreak(self):
        from ai_stock.dataflows.pipeline_data import _rank_board_leaders
        stocks = [
            self._stock("600001", "缩量", 7.0, vol_ratio=0.8),
            self._stock("600002", "放量", 7.0, vol_ratio=2.5),
        ]
        leaders = _rank_board_leaders(stocks, 1.0, "", 5)
        assert leaders[0]["code"] == "600002"

    def test_fetch_parses_fields_and_ranks(self, monkeypatch):
        from ai_stock.dataflows import pipeline_data as pd

        payload = {"data": {"diff": [
            {"f12": "000063", "f14": "中兴通讯", "f3": 5.1, "f8": 3.2,
             "f10": 1.8, "f20": 1.5e11, "f62": 2.0e8},
            {"f12": "002396", "f14": "星网锐捷", "f3": 4.0, "f8": 5.0,
             "f10": 2.0, "f20": 3e10, "f62": 5.0e7},
        ]}}
        response = type("R", (), {"json": lambda self: payload})()
        monkeypatch.setattr(pd, "_push2_get", lambda path, params: response)

        leaders = pd.get_industry_leader_stocks(
            "BK1033", top_n=5, board_change_pct=2.0, top_stock_code="000063",
        )
        assert [s["code"] for s in leaders] == ["000063", "002396"]
        first = leaders[0]
        assert first["leader_label"] == "领涨"
        assert first["turnover_rate"] == 3.2
        assert first["volume_ratio"] == 1.8
        assert first["main_net_inflow"] == 2.0e8
        assert first["market_cap"] == 1.5e11
        assert leaders[1]["leader_label"] == "龙头"

    def test_fetch_failure_returns_empty(self, monkeypatch):
        from ai_stock.dataflows import pipeline_data as pd

        def boom(path, params):
            raise RuntimeError("rate limited")

        monkeypatch.setattr(pd, "_push2_get", boom)
        assert pd.get_industry_leader_stocks("BK1033") == []

@pytest.mark.unit
class TestBuildIndustryRankingLeaders:
    """每个上榜行业都挂龙头（方案 A）；候选池注入仍只取 Top-3。"""

    @staticmethod
    def _news(score, industry):
        return {
            "composite_score": score, "bull_bear_bias": "bullish",
            "primary_industry": industry, "secondary_industry": "",
        }

    @staticmethod
    def _flow(name, code, inflow, change, top_code, top_name, top_pct):
        return {
            "name": name, "code": code, "main_net_inflow": inflow,
            "change_pct": change, "top_stock_code": top_code,
            "top_stock_name": top_name, "top_stock_pct": top_pct,
            "board_level": "industry",
        }

    def test_leaders_attached_to_every_ranked_board(self, monkeypatch):
        from ai_stock.pipeline import pipeline as pipe
        from ai_stock.dataflows import pipeline_data as pd

        news = [
            self._news(9.0, "电子"), self._news(8.0, "半导体"),
            self._news(7.0, "存储芯片"), self._news(6.0, "机器人"),
            self._news(5.0, "光伏"),
        ]
        flows = [
            self._flow("电子元件", "BK1033", 5e9, 2.0, "000063", "中兴通讯", 5.1),
            self._flow("半导体", "BK1036", 4e9, 1.5, "688981", "中芯国际", 4.2),
            self._flow("存储芯片", "BK1137", 3e9, 3.1, "603986", "兆易创新", 6.0),
            self._flow("机器人概念", "BK1108", 2e9, 1.0, "002747", "埃斯顿", 3.3),
            self._flow("光伏设备", "BK1031", 1e9, 0.5, "601012", "隆基绿能", 2.0),
        ]
        monkeypatch.setattr(pd, "get_all_board_fund_flow", lambda: flows)
        monkeypatch.setattr(
            pd, "get_industry_leader_stocks",
            lambda board_code, top_n=5, board_change_pct=None, top_stock_code="":
            [{"code": f"{board_code}01", "name": f"{board_code}龙头",
              "change_pct": 5.0, "turnover_rate": 1.0, "volume_ratio": 1.0,
              "market_cap": 0.0, "main_net_inflow": 0.0, "leader_label": "龙头"}],
        )

        rankings, industry_leaders = pipe._build_industry_ranking(news, None, top_n=5)

        assert len(rankings) == 5
        for row in rankings:
            assert row["leader_stocks"], f"rank {row['rank']} 缺龙头数据"
            assert row["leader_stocks"][0]["leader_label"] == "龙头"
        # 候选池注入只保留 Top-3 行业，且每个行业去重后各 1 条
        assert {li["industry"] for li in industry_leaders} == {
            "电子", "半导体", "存储芯片",
        }
        assert len(industry_leaders) == 3

    def test_fallback_top_gainer_when_fetch_fails(self, monkeypatch):
        from ai_stock.pipeline import pipeline as pipe
        from ai_stock.dataflows import pipeline_data as pd

        news = [self._news(9.0, "电子")]
        flows = [self._flow("电子元件", "BK1033", 5e9, 2.0, "000063", "中兴通讯", 5.1)]
        monkeypatch.setattr(pd, "get_all_board_fund_flow", lambda: flows)
        monkeypatch.setattr(pd, "get_industry_leader_stocks", lambda *a, **k: [])

        rankings, industry_leaders = pipe._build_industry_ranking(news, None, top_n=5)

        assert len(rankings) == 1
        leaders = rankings[0]["leader_stocks"]
        assert leaders and leaders[0]["code"] == "000063"
        assert leaders[0]["leader_label"] == "领涨"
        # 兜底用的是领涨股自身涨幅，而非板块涨幅
        assert leaders[0]["change_pct"] == 5.1
        # 兜底股仍可进入候选池（rank 1 ≤ 3）
        assert len(industry_leaders) == 1

    def test_no_board_code_skips_fetch_uses_top_gainer(self, monkeypatch):
        from ai_stock.pipeline import pipeline as pipe
        from ai_stock.dataflows import pipeline_data as pd

        news = [self._news(9.0, "电子")]
        # 无匹配板块 → industry_code 为空，get_industry_leader_stocks 不应被调用
        monkeypatch.setattr(pd, "get_all_board_fund_flow", lambda: [])
        called = []

        def spy(board_code, top_n=5, board_change_pct=None, top_stock_code=""):
            called.append(board_code)
            return [{"code": "000001", "name": "不该出现", "change_pct": 1.0,
                     "leader_label": "龙头"}]

        monkeypatch.setattr(pd, "get_industry_leader_stocks", spy)

        rankings, _ = pipe._build_industry_ranking(news, None, top_n=5)

        assert len(rankings) == 1
        assert rankings[0]["industry_code"] == ""
        assert called == []  # 无板块代码时不发多余请求
        # 无兜底可用 → 龙头列表为空而非报错
        assert rankings[0]["leader_stocks"] == []
