"""Tests for pipeline scoring, ranking, and candidate pool modules."""

import pytest
from unittest.mock import MagicMock

from ai_stock.pipeline.config import AGENT_WEIGHTS, MIN_COMPOSITE_SCORE
from ai_stock.pipeline.llm_judge import (
    AgentScoreResult,
    _parse_score_from_text,
    _parse_batch_from_text,
)
from ai_stock.pipeline.scoring import ScoredNews, score_all_news
from ai_stock.pipeline.ranking import rank_top20, _extract_sd_strength, build_top20_json
from ai_stock.pipeline.supply_demand import (
    SupplyDemandResult,
    _derive_from_agent_signals,
    _GAP_MATRIX,
)
from ai_stock.pipeline.candidate_pool import generate_candidate_pool, _add_candidate
from ai_stock.pipeline.cache import PipelineCache


@pytest.mark.unit
class TestAgentScoreResult:
    def test_defaults(self):
        r = AgentScoreResult(score=7.5, reasoning="test")
        assert r.score == 7.5
        assert r.industries == []
        assert r.top_stocks == []
        assert r.supply_demand_signal == "neutral"


@pytest.mark.unit
class TestParseScoreFromText:
    def test_json_extraction(self):
        text = '{"score": 8.5, "reasoning": "good"}'
        result = _parse_score_from_text(text)
        assert result.score == 8.5

    def test_keyword_extraction(self):
        text = "评分：7.2 分，因为政策利好"
        result = _parse_score_from_text(text)
        assert abs(result.score - 7.2) < 0.01

    def test_fallback(self):
        result = _parse_score_from_text("no useful info")
        assert result.score == 5.0


@pytest.mark.unit
class TestParseBatchFromText:
    def test_json_array(self):
        text = '[{"score": 7, "reasoning": "a"}, {"score": 8, "reasoning": "b"}]'
        results = _parse_batch_from_text(text, 2)
        assert len(results) == 2
        assert results[0].score == 7.0
        assert results[1].score == 8.0

    def test_fallback_padding(self):
        results = _parse_batch_from_text("garbage", 3)
        assert len(results) == 3
        assert all(r.score == 5.0 for r in results)


@pytest.mark.unit
class TestSupplyDemand:
    def test_derive_shortage(self):
        signals = ["demand_surge", "supply_shrink", "demand_surge", "neutral"]
        result = _derive_from_agent_signals(signals)
        assert result.gap_type in ("shortage", "demand_shortage")
        assert result.elasticity_coefficient > 0

    def test_derive_balanced(self):
        signals = ["neutral", "neutral", "neutral", "neutral"]
        result = _derive_from_agent_signals(signals)
        assert result.gap_type == "balanced"

    def test_gap_matrix_coverage(self):
        # All matrix entries should be defined
        for demand in ("demand_increase", "demand_decrease", "demand_neutral"):
            for supply in ("supply_increase", "supply_decrease", "supply_neutral"):
                assert (demand, supply) in _GAP_MATRIX


@pytest.mark.unit
class TestRanking:
    def test_rank_top20_basic(self):
        news = [
            {"title": f"news_{i}", "composite_score": 8.0 - i * 0.1,
             "bull_bear_bias": "bullish", "supply_demand_json": ""}
            for i in range(25)
        ]
        result = rank_top20(news, top_n=20)
        assert len(result) == 20
        assert result[0]["rank"] == 1
        assert result[0]["composite_score"] >= result[1]["composite_score"]

    def test_bearish_filtered(self):
        news = [
            {"title": "bull", "composite_score": 8.0, "bull_bear_bias": "bullish",
             "supply_demand_json": ""},
            {"title": "bear", "composite_score": 9.0, "bull_bear_bias": "bearish",
             "supply_demand_json": ""},
        ]
        result = rank_top20(news)
        assert len(result) == 1
        assert result[0]["title"] == "bull"

    def test_all_bearish_fallback(self):
        news = [
            {"title": f"bear_{i}", "composite_score": 7.0 - i * 0.1,
             "bull_bear_bias": "bearish", "supply_demand_json": ""}
            for i in range(10)
        ]
        result = rank_top20(news)
        assert len(result) <= 5  # fallback keeps top 5

    def test_extract_sd_strength(self):
        import json
        sd = json.dumps({"gap_type": "shortage", "elasticity_coefficient": 0.8})
        assert _extract_sd_strength(sd) > 0
        assert _extract_sd_strength("") == 0.0
        assert _extract_sd_strength("invalid") == 0.0

    def test_build_top20_json(self):
        ranked = [
            {"rank": 1, "title": "test", "composite_score": 8.5,
             "bull_bear_bias": "bullish", "industries": ["AI"],
             "expected_gain_low": 2.0, "expected_gain_high": 8.0},
        ]
        result = build_top20_json(ranked)
        import json
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["rank"] == 1


@pytest.mark.unit
class TestCandidatePool:
    def test_add_candidate_new(self):
        pool = {}
        _add_candidate(pool, "000001", name="测试", tier="P0",
                       event_title="事件A", industries=["AI"])
        assert "000001" in pool
        assert pool["000001"]["source_tier"] == "P0"
        assert pool["000001"]["event_match_count"] == 1

    def test_add_candidate_upgrade(self):
        pool = {}
        _add_candidate(pool, "000001", tier="P2", event_title="事件A")
        _add_candidate(pool, "000001", tier="P0", event_title="事件B")
        assert pool["000001"]["source_tier"] == "P0"
        assert pool["000001"]["event_match_count"] == 2

    def test_generate_empty(self):
        result = generate_candidate_pool([], [], MagicMock())
        assert result == []


@pytest.mark.unit
class TestCache:
    def test_set_get(self):
        cache = PipelineCache(ttl=3600)
        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"

    def test_miss(self):
        cache = PipelineCache()
        assert cache.get("nonexistent") is None

    def test_clear(self):
        cache = PipelineCache()
        cache.set("a", 1)
        cache.set("b", 2)
        assert cache.size == 2
        cache.clear_all()
        assert cache.size == 0

    def test_invalidate(self):
        cache = PipelineCache()
        cache.set("key", "val")
        cache.invalidate("key")
        assert cache.get("key") is None
