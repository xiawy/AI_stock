"""4-agent parallel news scoring + composite score calculation.

Each news item is scored independently by 4 agents:
- Policy analyst (政策分析师) — weight 0.30
- News analyst (新闻分析师) — weight 0.25
- Capital tracker (游资追踪) — weight 0.25
- Sentiment analyst (舆情分析师) — weight 0.20

The composite score determines whether a news item enters the candidate pool.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .config import (
    AGENT_WEIGHTS,
    MIN_COMPOSITE_SCORE,
    SCORING_BATCH_SIZE,
    SCORING_MAX_WORKERS,
)
from .llm_judge import AgentScoreResult, batch_score
from .cache import get_cache
from . import evolution as pipeline_evolution

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


class ScoredNews:
    """A news item with all 4 agent scores and the composite."""

    __slots__ = (
        "news", "policy", "news_agent", "capital", "sentiment",
        "composite", "industries", "top_stocks", "supply_demand_signals",
    )

    def __init__(self, news: dict):
        self.news = news
        self.policy: AgentScoreResult | None = None
        self.news_agent: AgentScoreResult | None = None
        self.capital: AgentScoreResult | None = None
        self.sentiment: AgentScoreResult | None = None
        self.composite: float = 0.0
        self.industries: list[str] = []
        self.top_stocks: list[dict] = []
        self.supply_demand_signals: list[str] = []

    def to_dict(self) -> dict:
        return {
            **self.news,
            "policy_score": self.policy.score if self.policy else 0.0,
            "news_score": self.news_agent.score if self.news_agent else 0.0,
            "capital_score": self.capital.score if self.capital else 0.0,
            "sentiment_score": self.sentiment.score if self.sentiment else 0.0,
            "composite_score": round(self.composite, 2),
            "industries": self.industries,
            "top_stocks": self.top_stocks,
            "supply_demand_signals": self.supply_demand_signals,
        }


# ---------------------------------------------------------------------------
# Single-agent scoring over a batch of news
# ---------------------------------------------------------------------------


def _score_agent(
    agent_role: str,
    news_items: list[dict],
    llm: Any,
) -> dict[str, AgentScoreResult]:
    """Run one agent over all news items (in sub-batches).

    Returns a mapping from title_hash → AgentScoreResult.
    """
    results: dict[str, AgentScoreResult] = {}

    # Wrap LLM with per-agent evolution context (custom strategies + past episodes)
    evo_role = f"pipeline_{agent_role}"
    evo_llm = pipeline_evolution.wrap_llm(llm, evo_role)

    # Split into sub-batches
    for i in range(0, len(news_items), SCORING_BATCH_SIZE):
        sub_batch = news_items[i : i + SCORING_BATCH_SIZE]
        try:
            scored = batch_score(agent_role, sub_batch, evo_llm)
            for item, score_result in zip(sub_batch, scored):
                results[item.get("title_hash", "")] = score_result
        except Exception as exc:
            logger.warning(
                "Agent %s sub-batch %d failed: %s", agent_role, i, exc,
            )
            # Fill with defaults so downstream doesn't break
            for item in sub_batch:
                results[item.get("title_hash", "")] = AgentScoreResult(
                    score=5.0, reasoning=f"评分失败: {exc}",
                )

    return results


# ---------------------------------------------------------------------------
# 4-agent parallel scoring
# ---------------------------------------------------------------------------


def score_all_news(
    news_items: list[dict],
    llm: Any,
    min_score: float = MIN_COMPOSITE_SCORE,
) -> list[ScoredNews]:
    """Score all news items with 4 parallel agents.

    Args:
        news_items: List of news dicts (from get_impact_news).
        llm: The LLM instance to use for scoring.
        min_score: Minimum composite score to enter the candidate pool.

    Returns:
        List of ScoredNews that pass the threshold, sorted by composite desc.
    """
    if not news_items:
        return []

    logger.info("Scoring %d news items with 4 agents (max_workers=%d)", len(news_items), SCORING_MAX_WORKERS)

    cache = get_cache()

    # Check cache: skip news items already scored within the TTL window
    uncached_items: list[dict] = []
    cached_results: dict[str, dict[str, AgentScoreResult]] = {}  # title_hash -> {role -> result}
    for news in news_items:
        th = news.get("title_hash", "")
        if not th:
            uncached_items.append(news)
            continue
        roles_cache: dict[str, AgentScoreResult] = {}
        all_hit = True
        for role in AGENT_WEIGHTS:
            key = f"score:{role}:{th}"
            val = cache.get(key)
            if val is not None:
                roles_cache[role] = val
            else:
                all_hit = False
        if all_hit and roles_cache:
            cached_results[th] = roles_cache
        else:
            uncached_items.append(news)

    logger.info("Cache: %d hits, %d need scoring", len(cached_results), len(uncached_items))

    # Run 4 agents in parallel on uncached items only
    agent_results: dict[str, dict[str, AgentScoreResult]] = {}

    if uncached_items:
        with ThreadPoolExecutor(max_workers=SCORING_MAX_WORKERS) as executor:
            futures = {}
            for role in AGENT_WEIGHTS:
                future = executor.submit(_score_agent, role, uncached_items, llm)
                futures[future] = role

            for future in as_completed(futures):
                role = futures[future]
                try:
                    agent_results[role] = future.result()
                except Exception as exc:
                    logger.error("Agent %s failed entirely: %s", role, exc)
                    agent_results[role] = {}

        # Populate cache with newly scored results
        for role, results_map in agent_results.items():
            for th_key, result in results_map.items():
                if th_key:
                    cache.set(f"score:{role}:{th_key}", result)

    # Merge cached + fresh results into ScoredNews
    scored_list: list[ScoredNews] = []
    for news in news_items:
        th = news.get("title_hash", "")
        sn = ScoredNews(news)

        # Cached results take priority; fall back to fresh results
        sn.policy = cached_results.get(th, {}).get("policy") or agent_results.get("policy", {}).get(th)
        sn.news_agent = cached_results.get(th, {}).get("news") or agent_results.get("news", {}).get(th)
        sn.capital = cached_results.get(th, {}).get("capital") or agent_results.get("capital", {}).get(th)
        sn.sentiment = cached_results.get(th, {}).get("sentiment") or agent_results.get("sentiment", {}).get(th)

        # Ensure we have at least default scores
        if sn.policy is None:
            sn.policy = AgentScoreResult(score=5.0, reasoning="无评分")
        if sn.news_agent is None:
            sn.news_agent = AgentScoreResult(score=5.0, reasoning="无评分")
        if sn.capital is None:
            sn.capital = AgentScoreResult(score=5.0, reasoning="无评分")
        if sn.sentiment is None:
            sn.sentiment = AgentScoreResult(score=5.0, reasoning="无评分")

        # Weighted composite score
        sn.composite = (
            AGENT_WEIGHTS["policy"] * sn.policy.score
            + AGENT_WEIGHTS["news"] * sn.news_agent.score
            + AGENT_WEIGHTS["capital"] * sn.capital.score
            + AGENT_WEIGHTS["sentiment"] * sn.sentiment.score
        )

        # Merge industries (deduplicated, preserving order)
        seen_industries: set[str] = set()
        for agent_result in (sn.policy, sn.news_agent, sn.capital, sn.sentiment):
            for ind in agent_result.industries:
                if ind and ind not in seen_industries:
                    seen_industries.add(ind)
                    sn.industries.append(ind)

        # Merge top_stocks (by elasticity desc, deduplicated by code)
        stock_map: dict[str, dict] = {}
        for agent_result in (sn.policy, sn.news_agent, sn.capital, sn.sentiment):
            for stock in agent_result.top_stocks:
                code = stock.get("code", "")
                if code and code not in stock_map:
                    stock_map[code] = stock
                elif code:
                    # Keep the one with higher elasticity
                    existing_elasticity = stock_map[code].get("elasticity", 0)
                    new_elasticity = stock.get("elasticity", 0)
                    if new_elasticity > existing_elasticity:
                        stock_map[code] = stock
        sn.top_stocks = sorted(
            stock_map.values(),
            key=lambda s: s.get("elasticity", 0),
            reverse=True,
        )[:5]

        # Collect supply-demand signals
        sn.supply_demand_signals = [
            agent_result.supply_demand_signal
            for agent_result in (sn.policy, sn.news_agent, sn.capital, sn.sentiment)
        ]

        scored_list.append(sn)

    # Filter by minimum score and sort
    candidates = [sn for sn in scored_list if sn.composite >= min_score]
    candidates.sort(key=lambda sn: sn.composite, reverse=True)

    logger.info(
        "Scoring complete: %d/%d items pass threshold %.1f",
        len(candidates), len(scored_list), min_score,
    )

    return candidates


def filter_candidates(
    scored_news: list[ScoredNews],
    min_score: float = MIN_COMPOSITE_SCORE,
) -> list[ScoredNews]:
    """Re-filter scored news by composite score threshold."""
    return [sn for sn in scored_news if sn.composite >= min_score]
