"""Candidate pool generation for stock recommendation.

Combines Top 5 bullish events' affected stocks with recent limit-up stocks
and the industry board's Top-3 leader stocks (行业榜联动) to build a
candidate pool of 30-50 stocks for further scoring.

Priority tiers:
- P0: Event top_stocks ∩ limit-up stocks (direct hit)
      + industry-board leaders (行业榜龙头, sector-beta plays)
- P1: Event industries ∩ limit-up stocks (industry beta)
- P2: Supply chain / indirect beneficiaries (LLM-assisted)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .config import MAX_CANDIDATES, MIN_CANDIDATES, TOP_N_EVENTS_FOR_CANDIDATES
from . import evolution as pipeline_evolution

logger = logging.getLogger(__name__)


def generate_candidate_pool(
    top_events: list[dict],
    limit_up_stocks: list[dict],
    llm: Any,
    industry_leaders: list[dict] | None = None,
) -> list[dict]:
    """Generate a candidate stock pool from events + limit-up + industry data.

    Args:
        top_events: Top N events from the impact ranking (with industries,
            top_stocks, supply_demand_json).
        limit_up_stocks: Recent limit-up stocks from get_limit_up_stocks.
        llm: LLM for supply-chain association (P2 tier).
        industry_leaders: Leader stocks from the industry board's Top-3 hot
            industries (行业榜龙头), each {code, name, industry, rank}. They
            enter at tier P0 — most excess return comes from sector beta.

    Returns:
        List of candidate dicts, each with:
        - code, name
        - source_tier (P0/P1/P2)
        - matched_events: list of event titles
        - matched_industries: list of industries
        - event_match_count: how many events reference this stock
    """
    # Index limit-up stocks by code
    lu_by_code: dict[str, dict] = {}
    for s in limit_up_stocks:
        code = s.get("code", "")
        if code:
            lu_by_code[code] = s

    # Index limit-up stocks by reason tags (for P1 matching)
    lu_by_tag: dict[str, list[dict]] = {}
    for s in limit_up_stocks:
        for tag in s.get("reason_tags", []):
            lu_by_tag.setdefault(tag, []).append(s)

    # Use only the top N events
    events = top_events[:TOP_N_EVENTS_FOR_CANDIDATES]

    candidates: dict[str, dict] = {}  # code -> candidate dict

    # --- P0 (industry): 行业榜 Top-3 龙头股 — sector-beta injection ---
    # Most excess return comes from industry beta, not stock alpha: leaders of
    # the hottest industries enter the pool at the same tier as direct event
    # hits so the stock scoring/debate stages can pick them up.
    for leader in industry_leaders or []:
        code = leader.get("code", "")
        if not code:
            continue
        industry = leader.get("industry", "")
        _add_candidate(
            candidates, code,
            name=leader.get("name", ""),
            tier="P0",
            event_title=f"行业榜Top{leader.get('rank', 3)}·{industry}" if industry else "行业榜龙头",
            industries=[industry] if industry else [],
            elasticity=0.6,
        )

    # --- P0: Direct hit — event top_stocks that are also limit-up ---
    for event in events:
        top_stocks = event.get("top_stocks", [])
        if isinstance(top_stocks, str):
            try:
                top_stocks = json.loads(top_stocks)
            except (json.JSONDecodeError, TypeError):
                top_stocks = []

        for stock in top_stocks:
            code = stock.get("code", "")
            if not code:
                continue
            if code in lu_by_code:
                _add_candidate(
                    candidates, code,
                    name=stock.get("name", "") or lu_by_code[code].get("name", ""),
                    tier="P0",
                    event_title=event.get("title", ""),
                    industries=event.get("industries", []),
                    elasticity=stock.get("elasticity", 0.5),
                )

    # --- P1: Industry beta — limit-up stocks in event-affected industries ---
    for event in events:
        industries = event.get("industries", [])
        if isinstance(industries, str):
            try:
                industries = json.loads(industries)
            except (json.JSONDecodeError, TypeError):
                industries = []

        for industry in industries:
            # Match limit-up stocks whose reason_tags contain the industry
            for tag, stocks in lu_by_tag.items():
                if industry in tag or tag in industry:
                    for s in stocks:
                        code = s.get("code", "")
                        if code and code not in candidates:
                            _add_candidate(
                                candidates, code,
                                name=s.get("name", ""),
                                tier="P1",
                                event_title=event.get("title", ""),
                                industries=[industry],
                                elasticity=0.3,
                            )

    # --- P2: Supply chain / indirect beneficiaries (LLM-assisted) ---
    if len(candidates) < MIN_CANDIDATES:
        p2_candidates = _find_p2_candidates(events, lu_by_code, candidates, llm)
        for code, info in p2_candidates.items():
            if code not in candidates:
                _add_candidate(
                    candidates, code,
                    name=info.get("name", ""),
                    tier="P2",
                    event_title=info.get("event_title", ""),
                    industries=info.get("industries", []),
                    elasticity=info.get("elasticity", 0.2),
                )

    # --- Fill remaining slots from limit-up stocks if still under MIN ---
    if len(candidates) < MIN_CANDIDATES:
        for code, s in lu_by_code.items():
            if code not in candidates:
                _add_candidate(
                    candidates, code,
                    name=s.get("name", ""),
                    tier="P2",
                    event_title="近期涨停股",
                    industries=s.get("reason_tags", []),
                    elasticity=0.1,
                )
            if len(candidates) >= MAX_CANDIDATES:
                break

    # Sort by tier (P0 first) then event_match_count desc, cap at MAX
    result = sorted(
        candidates.values(),
        key=lambda c: (
            {"P0": 0, "P1": 1, "P2": 2}.get(c["source_tier"], 3),
            -c["event_match_count"],
            -c.get("elasticity", 0),
        ),
    )[:MAX_CANDIDATES]

    logger.info(
        "Candidate pool: %d stocks (P0=%d, P1=%d, P2=%d)",
        len(result),
        sum(1 for c in result if c["source_tier"] == "P0"),
        sum(1 for c in result if c["source_tier"] == "P1"),
        sum(1 for c in result if c["source_tier"] == "P2"),
    )

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _add_candidate(
    candidates: dict[str, dict],
    code: str,
    name: str = "",
    tier: str = "P2",
    event_title: str = "",
    industries: list[str] | None = None,
    elasticity: float = 0.3,
) -> None:
    """Add or update a candidate in the pool."""
    if code in candidates:
        candidates[code]["matched_events"].append(event_title)
        candidates[code]["event_match_count"] += 1
        if industries:
            existing = set(candidates[code]["matched_industries"])
            for ind in industries:
                if ind not in existing:
                    candidates[code]["matched_industries"].append(ind)
                    existing.add(ind)
        # Upgrade tier if better
        tier_rank = {"P0": 0, "P1": 1, "P2": 2}
        if tier_rank.get(tier, 3) < tier_rank.get(candidates[code]["source_tier"], 3):
            candidates[code]["source_tier"] = tier
        # Keep higher elasticity
        candidates[code]["elasticity"] = max(
            candidates[code].get("elasticity", 0), elasticity,
        )
    else:
        candidates[code] = {
            "code": code,
            "name": name,
            "source_tier": tier,
            "matched_events": [event_title] if event_title else [],
            "matched_industries": list(industries) if industries else [],
            "event_match_count": 1 if event_title else 0,
            "elasticity": elasticity,
        }


def _find_p2_candidates(
    events: list[dict],
    lu_by_code: dict[str, dict],
    existing: dict[str, dict],
    llm: Any,
) -> dict[str, dict]:
    """Use LLM to find P2 supply-chain candidates from limit-up stocks."""
    if not events or not lu_by_code:
        return {}

    # Build a summary of events and available limit-up stocks
    event_summary = []
    for e in events:
        industries = e.get("industries", [])
        if isinstance(industries, str):
            try:
                industries = json.loads(industries)
            except (json.JSONDecodeError, TypeError):
                industries = []
        event_summary.append(
            f"- {e.get('title', '')} | 行业: {', '.join(industries[:5])}"
        )

    lu_sample = list(lu_by_code.values())[:30]
    lu_summary = "\n".join(
        f"- {s.get('code', '')} {s.get('name', '')} | "
        f"标签: {', '.join(s.get('reason_tags', []))}"
        for s in lu_sample
    )

    existing_codes = set(existing.keys())

    prompt = (
        "你是一位A股产业链分析师。以下是今日 Top 利好事件和近期涨停股列表。\n\n"
        f"## Top 利好事件\n" + "\n".join(event_summary) + "\n\n"
        f"## 近期涨停股\n{lu_summary}\n\n"
        "请从中找出与上述事件存在产业链关联（上下游、替代品、配套服务）的个股，"
        "作为间接受益标的。\n"
        "排除已在候选池中的股票。\n\n"
        "以 JSON 数组格式输出，每个元素：\n"
        '{"code": "000001", "name": "xxx", "event_title": "关联事件", '
        '"industries": ["行业"], "elasticity": 0.3}\n'
        "最多输出 15 支。"
    )

    try:
        # Wrap LLM with evolution context (custom strategies + past episodes)
        evo_llm = pipeline_evolution.wrap_llm(llm, "pipeline_candidate_pool")
        response = evo_llm.invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        return _parse_p2_results(text, existing_codes)
    except Exception as exc:
        logger.warning("P2 LLM association failed: %s", exc)
        return {}


def _parse_p2_results(text: str, exclude_codes: set[str]) -> dict[str, dict]:
    """Parse P2 candidates from LLM output."""
    import re

    results: dict[str, dict] = {}

    # Try JSON array extraction
    json_match = re.search(r"\[[\s\S]*?\]", text)
    if json_match:
        try:
            data = json.loads(json_match.group())
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        code = item.get("code", "")
                        if code and code not in exclude_codes:
                            results[code] = {
                                "name": item.get("name", ""),
                                "event_title": item.get("event_title", ""),
                                "industries": item.get("industries", []),
                                "elasticity": float(item.get("elasticity", 0.2)),
                            }
                return results
        except (json.JSONDecodeError, TypeError):
            pass

    return results
