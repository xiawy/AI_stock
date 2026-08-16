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
        "composite", "primary_industry", "secondary_industry",
        "industries", "top_stocks", "supply_demand_signals",
        "_supply_demand_result",
    )

    def __init__(self, news: dict):
        self.news = news
        self.policy: AgentScoreResult | None = None
        self.news_agent: AgentScoreResult | None = None
        self.capital: AgentScoreResult | None = None
        self.sentiment: AgentScoreResult | None = None
        self.composite: float = 0.0
        self.primary_industry: str = ""
        self.secondary_industry: str = ""
        self.industries: list[str] = []
        self.top_stocks: list[dict] = []
        self.supply_demand_signals: list[str] = []
        # Attached later by the pipeline (Step 5); must be a slot or the
        # assignment raises AttributeError on this __slots__ class.
        self._supply_demand_result = None

    def to_dict(self) -> dict:
        return {
            **self.news,
            "policy_score": self.policy.score if self.policy else 0.0,
            "news_score": self.news_agent.score if self.news_agent else 0.0,
            "capital_score": self.capital.score if self.capital else 0.0,
            "sentiment_score": self.sentiment.score if self.sentiment else 0.0,
            "composite_score": round(self.composite, 2),
            "primary_industry": self.primary_industry,
            "secondary_industry": self.secondary_industry,
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

        # Merge primary/secondary industry labels across the 4 agents.
        # Cross-agent agreement beats any single agent's opinion; ties are
        # broken by role priority (policy > capital > sentiment > news).
        sn.primary_industry, sn.secondary_industry = _merge_industry_labels(
            sn.policy, sn.news_agent, sn.capital, sn.sentiment,
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


# ---------------------------------------------------------------------------
# Industry label merging
# ---------------------------------------------------------------------------

# Role priority for tie-breaks: policy news has the most authoritative
# industry attribution, capital-flow agents know where money actually goes.
_INDUSTRY_VOTE_PRIORITY = ("policy", "capital", "sentiment", "news")
# Call order of _merge_industry_labels: (policy, news, capital, sentiment).
_LABEL_CALL_ROLES = ("policy", "news", "capital", "sentiment")


def _merge_industry_labels(
    *agent_results: AgentScoreResult | None,
) -> tuple[str, str]:
    """Merge primary/secondary industry labels across the 4 agents.

    Strategy: count primary votes first, then secondary votes (excluding the
    chosen primary). A label needs >= 2 votes to win outright; otherwise the
    highest-priority agent's (policy > capital > sentiment > news) non-empty
    label is used. Returns (primary, secondary).
    """
    # Args arrive in call order (policy, news, capital, sentiment); map them
    # back to priority order for the tie-break fallback.
    role_by_result = {
        id(r): _INDUSTRY_VOTE_PRIORITY.index(role)
        for role, r in zip(_LABEL_CALL_ROLES, agent_results)
    }

    primary_votes: dict[str, int] = {}
    secondary_votes: dict[str, int] = {}
    for result in agent_results:
        if result is None:
            continue
        if result.primary_industry:
            primary_votes[result.primary_industry] = (
                primary_votes.get(result.primary_industry, 0) + 1
            )
        if result.secondary_industry:
            secondary_votes[result.secondary_industry] = (
                secondary_votes.get(result.secondary_industry, 0) + 1
            )

    # Fallback: LLMs (especially in batch structured output) often leave
    # primary/secondary empty while still filling ``industries``. Materialise
    # each agent's industries[0] / industries[1] as its implicit primary /
    # secondary vote so the industry heatmap never starves on missing labels.
    if not primary_votes and not secondary_votes:
        for result in agent_results:
            if result is None:
                continue
            inds = [i.strip() for i in result.industries if i and i.strip()]
            if not inds:
                continue
            if not result.primary_industry:
                result.primary_industry = inds[0]
            if not result.secondary_industry and len(inds) >= 2:
                result.secondary_industry = inds[1]
            primary_votes[result.primary_industry] = (
                primary_votes.get(result.primary_industry, 0) + 1
            )
            if result.secondary_industry:
                secondary_votes[result.secondary_industry] = (
                    secondary_votes.get(result.secondary_industry, 0) + 1
                )

    def _pick(votes: dict[str, int], exclude: str = "") -> str:
        # Majority label (>= 2 votes) wins; ties broken by priority vote order.
        best_label, best_count = "", 0
        for label, count in votes.items():
            if label == exclude:
                continue
            if count > best_count:
                best_label, best_count = label, count
        if best_count >= 2:
            return best_label
        # No majority: fall back to the highest-priority agent's opinion.
        for result in sorted(
            (r for r in agent_results if r is not None),
            key=lambda r: role_by_result[id(r)],
        ):
            for label in (result.primary_industry, result.secondary_industry):
                if label and label != exclude:
                    return label
        return ""

    primary = _pick(primary_votes)
    secondary = _pick(secondary_votes, exclude=primary)
    return primary, secondary


def filter_candidates(
    scored_news: list[ScoredNews],
    min_score: float = MIN_COMPOSITE_SCORE,
) -> list[ScoredNews]:
    """Re-filter scored news by composite score threshold."""
    return [sn for sn in scored_news if sn.composite >= min_score]
