"""Unit tests for the human-in-the-loop evolution review flow.

Uses temporary directories and a FakeLLM so nothing touches the real
custom_strategies/ tree or calls external APIs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_stock.evolution import review_service
from ai_stock.evolution.review_engine import ReviewEngine


class FakeLLM:
    """Minimal stand-in for a langchain chat model."""

    def __init__(self, text: str = "复盘总结：命中率尚可，需加强风控。\n\n改进建议：\n1. 收紧止损\n2. 控制仓位"):
        self.text = text

    def invoke(self, prompt, *args, **kwargs):
        class _Resp:
            content = ""

        _Resp.content = self.text
        return _Resp()


def _write_episode(path: Path, ep_id: str, outcome: str, rating: str = "buy") -> None:
    json.dump(
        {
            "id": ep_id,
            "ticker": "600000",
            "date": "2026-08-01",
            "agent": "market",
            "input_summary": "analysis of 600000",
            "output_summary": f"episode {ep_id}",
            "outcome": outcome,
            "rating": rating,
        },
        open(path, "w", encoding="utf-8"),
    )


@pytest.fixture()
def evo_config(tmp_path: Path) -> dict:
    """Config pointing at isolated temp directories (one agent with 2 episodes)."""
    strategies = tmp_path / "custom_strategies" / "market"
    strategies.mkdir(parents=True)
    strategies.joinpath("market_strategies1.md").write_text(
        "# 策略\n保持现有框架。", encoding="utf-8"
    )
    episodes = tmp_path / "evolution_data" / "market" / "episodes"
    episodes.mkdir(parents=True)
    _write_episode(episodes / "e1.json", "e1", outcome="success")
    _write_episode(episodes / "e2.json", "e2", outcome="failure")
    return {
        "evolution_base_dir": str(tmp_path / "evolution_data"),
        "custom_strategies_dir": str(tmp_path / "custom_strategies"),
        "learnings_dir": str(tmp_path / "learnings"),
        "evolution_top_k_episodes": 3,
    }


# ── ReviewEngine ────────────────────────────────────────────────


def test_review_engine_falls_back_to_pending_episodes(tmp_path: Path) -> None:
    """No resolved episodes → still reviews pending ones (degraded path)."""
    from ai_stock.evolution.memory_system import AgentMemorySystem

    strategies = tmp_path / "custom_strategies" / "market"
    strategies.mkdir(parents=True)
    episodes = tmp_path / "data" / "market" / "episodes"
    episodes.mkdir(parents=True)
    _write_episode(episodes / "p1.json", "p1", outcome="pending")

    memory = AgentMemorySystem(
        "market", tmp_path / "data", tmp_path / "custom_strategies"
    )
    engine = ReviewEngine("market", memory, FakeLLM(), tmp_path / "learnings")
    result = engine.run_review()

    assert result["episodes_total"] == 1
    assert result["episodes_pending"] == 1
    assert result["episodes_resolved"] == 0
    assert result["summary"]
    # A summary file should have been written.
    assert list((tmp_path / "learnings" / "market").glob("*_summary.md"))


def test_review_engine_no_episodes_returns_empty(tmp_path: Path) -> None:
    from ai_stock.evolution.memory_system import AgentMemorySystem

    memory = AgentMemorySystem(
        "market", tmp_path / "data", tmp_path / "custom_strategies"
    )
    engine = ReviewEngine("market", memory, FakeLLM(), tmp_path / "learnings")
    result = engine.run_review()
    assert result["episodes_total"] == 0
    assert result["suggestions"] == ""
    assert "No episodes" in result["summary"]


# ── review_service ──────────────────────────────────────────────


def test_run_review_for_agent_generates_draft(evo_config: dict) -> None:
    result = review_service.run_review_for_agent("market", evo_config, llm=FakeLLM())
    assert result["episodes_total"] == 2
    assert result["successes"] == 1
    assert result["failures"] == 1
    assert result["draft"] is not None

    drafts = review_service.list_drafts(evo_config)
    assert len(drafts) == 1
    assert drafts[0]["agent"] == "market"
    assert drafts[0]["filename"].startswith("draft_")


def test_approve_draft_applies_and_removes_from_queue(evo_config: dict) -> None:
    review_service.run_review_for_agent("market", evo_config, llm=FakeLLM())
    draft = review_service.list_drafts(evo_config)[0]

    result = review_service.approve_draft(evo_config, "market", draft["filename"])
    assert "已批准" in result["detail"]

    # Draft removed from the queue; strategy dir now holds the applied draft.
    assert review_service.list_drafts(evo_config) == []
    strategy_files = list(
        (Path(evo_config["custom_strategies_dir"]) / "market").glob("*.md")
    )
    assert any(p.name.startswith("strategy_") for p in strategy_files)


def test_reject_draft_deletes_and_keeps_strategy(evo_config: dict) -> None:
    review_service.run_review_for_agent("market", evo_config, llm=FakeLLM())
    draft = review_service.list_drafts(evo_config)[0]

    result = review_service.reject_draft(evo_config, "market", draft["filename"])
    assert "已拒绝" in result["detail"]
    assert review_service.list_drafts(evo_config) == []

    # Original strategy untouched.
    names = [
        p.name
        for p in (Path(evo_config["custom_strategies_dir"]) / "market").glob("*.md")
    ]
    assert "market_strategies1.md" in names


def test_draft_operations_on_missing_file(evo_config: dict) -> None:
    with pytest.raises(FileNotFoundError):
        review_service.get_draft(evo_config, "market", "draft_20260101_000000.md")
    with pytest.raises(FileNotFoundError):
        review_service.approve_draft(evo_config, "market", "draft_20260101_000000.md")
    with pytest.raises(FileNotFoundError):
        review_service.reject_draft(evo_config, "market", "draft_20260101_000000.md")


def test_validate_draft_filename_blocks_traversal() -> None:
    with pytest.raises(ValueError):
        review_service.validate_draft_filename("../evil.md")
    with pytest.raises(ValueError):
        review_service.validate_draft_filename("strategy_2026.md")
    assert (
        review_service.validate_draft_filename("draft_20260827_120000.md")
        == "draft_20260827_120000.md"
    )


def test_resolve_agents_unknown_raises() -> None:
    with pytest.raises(ValueError):
        review_service.resolve_agents("nope")
    assert review_service.resolve_agents("all") == review_service.AGENTS
    assert review_service.resolve_agents("market") == ["market"]


def test_run_review_unknown_agent_raises(evo_config: dict) -> None:
    with pytest.raises(ValueError):
        review_service.run_review_for_agent("nope", evo_config, llm=FakeLLM())
