"""Top 20 impact ranking — final sorting and filtering.

After scoring (Phase 2) and debate (Phase 3), this module produces the
definitive Top 20 ranking by combining composite score and supply-demand
strength, while filtering out bearish items. It also aggregates news
heat by industry into the industry heatmap (行业榜).
"""

from __future__ import annotations

import difflib
import json
import logging
from typing import Any

from .config import TOP_N_IMPACT
from .scoring import ScoredNews
from .supply_demand import SupplyDemandResult

logger = logging.getLogger(__name__)


def rank_top20(
    debated_news: list[dict],
    top_n: int = TOP_N_IMPACT,
) -> list[dict]:
    """Produce the final Top N impact ranking.

    Args:
        debated_news: List of news dicts with all scoring + debate fields populated.
            Each dict must have at minimum:
            - composite_score (float)
            - bull_bear_bias (str: "bullish" / "bearish" / "neutral")
            - supply_demand_json (str, JSON of SupplyDemandResult)
            - all other NewsItem fields
        top_n: Number of items in the final ranking (default 20).

    Returns:
        List of news dicts sorted by rank (1 = most impactful), with
        the ``rank`` field set.
    """
    if not debated_news:
        return []

    # 1. Filter: only keep bullish or neutral items
    candidates = [
        n for n in debated_news
        if n.get("bull_bear_bias", "neutral") in ("bullish", "neutral")
    ]

    if not candidates:
        logger.warning(
            "All %d debated news are bearish; keeping top 5 least-bearish as fallback",
            len(debated_news),
        )
        # Fallback: keep the least bearish ones so the pipeline still produces output
        candidates = sorted(
            debated_news,
            key=lambda n: n.get("composite_score", 0),
            reverse=True,
        )[:5]

    # 2. Sort by composite score (primary) and supply-demand strength (secondary)
    def _sort_key(news: dict) -> tuple[float, float]:
        score = news.get("composite_score", 0.0)
        # Extract supply-demand strength for secondary sort
        sd_strength = _extract_sd_strength(news.get("supply_demand_json", ""))
        return (score, sd_strength)

    candidates.sort(key=_sort_key, reverse=True)

    # 3. Assign ranks
    ranked = candidates[:top_n]
    for i, news in enumerate(ranked, 1):
        news["rank"] = i

    logger.info(
        "Top %d ranking produced from %d candidates "
        "(%d bearish filtered, %d total debated)",
        len(ranked), len(candidates),
        len(debated_news) - len(candidates),
        len(debated_news),
    )

    return ranked


def _extract_sd_strength(supply_demand_json: str) -> float:
    """Extract a numeric strength from supply-demand JSON for secondary sorting.

    Higher = stronger supply-demand gap (more impactful).
    """
    if not supply_demand_json:
        return 0.0
    try:
        data = json.loads(supply_demand_json)
        if not data:
            return 0.0

        # Map gap types to strength values
        gap_type = data.get("gap_type", "balanced")
        gap_strength = {
            "shortage": 3.0,          # 供不应求 — strongest
            "demand_shortage": 2.0,   # 需求增加
            "supply_surplus": 1.5,    # 供给收缩
            "balanced": 0.0,          # 平衡
            "surplus": -1.0,          # 供过于求
        }
        base = gap_strength.get(gap_type, 0.0)

        # Scale by elasticity
        elasticity = data.get("elasticity_coefficient", 0.5)
        return base * elasticity

    except (json.JSONDecodeError, TypeError):
        return 0.0


def build_top20_json(ranked_news: list[dict]) -> str:
    """Build a compact JSON summary of the Top 20 for storage in ImpactSnapshot.

    The full data lives in NewsItem rows; this is a quick-lookup summary.
    """
    summary = []
    for news in ranked_news:
        summary.append({
            "rank": news.get("rank", 0),
            "title": news.get("title", "")[:80],
            "composite_score": round(news.get("composite_score", 0), 2),
            "bull_bear_bias": news.get("bull_bear_bias", "neutral"),
            "primary_industry": news.get("primary_industry", ""),
            "secondary_industry": news.get("secondary_industry", ""),
            "industries": news.get("industries", []),
            "expected_gain_low": news.get("expected_gain_low", 0),
            "expected_gain_high": news.get("expected_gain_high", 0),
        })
    return json.dumps(summary, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Industry heatmap (行业榜) — news-driven industry heat aggregation
# ---------------------------------------------------------------------------

# Weight of a news item's contribution via its primary vs secondary industry.
PRIMARY_INDUSTRY_WEIGHT = 1.0
SECONDARY_INDUSTRY_WEIGHT = 0.5

# Bull/bear bias discount on industry heat: bearish news about an industry
# still signals attention but should not push it up the bullish ranking.
_BIAS_FACTOR = {"bullish": 1.0, "neutral": 0.6, "bearish": 0.2}

# Heat-score fraction above which an industry counts as "hot" for the
# resonance/rating logic (relative to the hottest industry of this run).
_HOT_HEAT_FRACTION = 0.5

# Fund-flow (relative to the largest absolute net inflow) above which money
# counts as "meaningfully flowing in".
_MEANINGFUL_INFLOW_FRACTION = 0.10


# ---------------------------------------------------------------------------
# Fine-grained (concept-level) industry mapping
# ---------------------------------------------------------------------------

# 细分概念板块别名表（语义种子，极小且不会随题材增长）。
#
# 为什么只需要这么点、且不需要为新题材继续加：
#   - 字符串层面的错位（存储→存储芯片、光模块→光通信模块、拼写/后缀差异、以及
#     未来出现的任何新题材）由 `_normalize_board_name` + 子串 + `difflib` 模糊匹配
#     自动桥接，因为东财板块名几乎总是和题材词本身接近。
#   - 这里只保留「完全没有公共子串、纯语义同义」的少数几条（封测→先进封装、
#     MLCC→陶瓷电容、内存/DRAM→存储芯片…），模糊匹配/子串都无能为力时才用。
#   - 语义归一的主力是 LLM 本身（prompt 里要求输出东财规范概念名），本表只是兜底。
# 键统一小写；值必须是东财概念板块的真实名称（已核验存在）。
_INDUSTRY_ALIASES: dict[str, str] = {
    # 半导体封测 / 封装
    "封测": "先进封装", "半导体封测": "先进封装", "集成电路封测": "先进封装",
    "封装测试": "先进封装", "芯片封测": "先进封装", "ic封装": "先进封装",
    "chiplet": "先进封装", "先进封装": "先进封装",
    # 被动元件 / MLCC
    "被动元件": "被动元件概念", "被动元器件": "被动元件概念",
    "被动元件概念": "被动元件概念",
    "mlcc": "MLCC", "mlcc概念": "MLCC", "陶瓷电容": "MLCC",
    # 存储 / 内存
    "存储": "存储芯片", "内存": "存储芯片", "存储芯片": "存储芯片",
    "dram": "存储芯片", "nand": "存储芯片", "闪存": "存储芯片",
    "hbm": "高带宽内存", "高带宽内存": "高带宽内存",
    # 光通信 / CPO / 算力
    "光模块": "光通信模块", "光通信": "光通信模块", "光器件": "光通信模块",
    "光模块概念": "光通信模块",
    "cpo": "CPO概念", "cpo概念": "CPO概念",
    "算力": "算力概念", "算力概念": "算力概念", "算力租赁": "算力概念",
    "ai芯片": "AI芯片", "人工智能芯片": "AI芯片", "ai算力芯片": "AI芯片",
    # PCB / 覆铜板
    "pcb": "PCB", "印制电路板": "PCB", "覆铜板": "PCB", "覆铜板ccl": "PCB",
    # 功率半导体 / 第三代半导体
    "igbt": "IGBT概念",
    "第三代半导体": "第三代半导体",
    "碳化硅": "碳化硅", "sic": "碳化硅",
    "氮化镓": "氮化镓", "gan": "氮化镓",
    # 散热 / 连接
    "液冷": "液冷服务器", "液冷服务器": "液冷服务器", "液冷散热": "液冷服务器",
    "铜缆": "铜缆高速连接", "高速连接": "铜缆高速连接", "铜连接": "铜缆高速连接",
    # 光刻 / 其他
    "光刻胶": "光刻胶", "光刻机": "光刻机",
    "人工智能": "人工智能", "ai": "人工智能",
}


def _normalize_board_name(name: str) -> str:
    """Lowercase a board/label name and strip common board suffixes.

    e.g. “被动元件概念” → “被动元件”, “CPO概念” → “cpo”, “电子元件” → “电子元件”.
    Bridges LLM free-form labels and Eastmoney board names (both directions).
    """
    norm = (name or "").strip().lower()
    for suffix in ("概念", "板块", "行业", "指数"):
        if norm.endswith(suffix):
            norm = norm[: -len(suffix)]
            break
    return norm


# difflib fuzzy threshold. High enough to avoid false positives, low enough to
# catch 光模块→光通信模块 (0.75) and 被动元器件→被动元件概念 (~0.73).
_FUZZY_CUTOFF = 0.72


def _fuzzy_match_board(
    label: str,
    flows_by_lower: dict[str, dict],
    board_names_lower: list[str],
) -> dict | None:
    """difflib fuzzy fallback: bridge string-level variants to a real board.

    Purely mechanical (no semantic knowledge) — handles typos, suffix drift and
    non-contiguous overlaps like 光模块→光通信模块. Brand-new themes whose board
    name is close to the theme word resolve here automatically, so the alias
    table above never needs to grow for new 题材.
    """
    if not board_names_lower or len(label) < 2:
        return None
    hit = difflib.get_close_matches(
        label.lower(), board_names_lower, n=1, cutoff=_FUZZY_CUTOFF
    )
    if hit:
        return flows_by_lower.get(hit[0])
    return None


def calculate_industry_heatmap(
    debated_news: list[dict],
    industry_flows: list[dict] | None = None,
    top_n: int = 10,
) -> list[dict]:
    """Aggregate debated news into an industry heat ranking (行业榜).

    Algorithm (加权热度聚合):
        heat(industry) = Σ_news composite_score × industry_weight × bias_factor
        - primary_industry contributes weight 1.0, secondary 0.5
        - bullish news counts fully, neutral 0.6, bearish 0.2

    Each aggregated industry is then matched against the realtime industry
    fund-flow board data (Eastmoney). When "舆论热度" (news heat) and
    "主力资金净流入" (main capital net inflow) align, the industry gets a
    higher resonance/rating; hot news + outflow = divergence warning.

    Pure function — no HTTP calls. Leader stocks are attached later by the
    pipeline (see ``pipeline.py`` Step 8).

    Args:
        debated_news: Ranked/debated news dicts with composite_score,
            bull_bear_bias, primary_industry, secondary_industry.
        industry_flows: Output of ``get_all_board_fund_flow()`` (行业 + 概念
            板块, optional; when None/empty the ranking degrades to news heat only).
        top_n: Number of industries to return (default 10).

    Returns:
        List of industry dicts sorted by heat_score desc, each with:
        industry (LLM 原始标签), board_name (东财规范板块名), industry_code
        (BKxxxx), industry_level (concept|industry), heat_score, news_count,
        fund_flow_net (元, None when unmatched), change_pct, resonance,
        rating, top_stock_*.
    """
    heat: dict[str, dict] = {}

    for news in debated_news:
        score = news.get("composite_score", 0.0)
        if score <= 0:
            continue
        bias = _BIAS_FACTOR.get(news.get("bull_bear_bias", "neutral"), 0.6)
        for industry, weight in (
            (news.get("primary_industry", ""), PRIMARY_INDUSTRY_WEIGHT),
            (news.get("secondary_industry", ""), SECONDARY_INDUSTRY_WEIGHT),
        ):
            industry = (industry or "").strip()
            if not industry:
                continue
            entry = heat.setdefault(
                industry,
                {"industry": industry, "heat_score": 0.0, "news_count": 0},
            )
            entry["heat_score"] += score * weight * bias
            entry["news_count"] += 1

    if not heat:
        return []

    # Fund-flow matching: exact name → alias (fine-grained concept) →
    # normalized (suffix-stripped) → bidirectional substring.
    # LLM outputs free-form labels like “封测” / “MLCC” / “存储”; Eastmoney
    # boards are named “先进封装” / “MLCC” / “存储芯片”. The alias table +
    # suffix-stripping bridge the two taxonomies so concept-level themes
    # (细分概念板块) can surface on the heatmap.
    flows_by_name: dict[str, dict] = {
        f.get("name", ""): f for f in (industry_flows or []) if f.get("name")
    }
    flows_by_norm: dict[str, dict] = {
        _normalize_board_name(f.get("name", "")): f
        for f in (industry_flows or [])
        if f.get("name")
    }
    # Lowercased view for the difflib fuzzy fallback (handles 光模块→光通信模块,
    # typos, and future/新兴题材 without a hardcoded alias entry).
    flows_by_lower: dict[str, dict] = {
        (f.get("name") or "").lower(): f
        for f in (industry_flows or [])
        if f.get("name")
    }
    board_names_lower = list(flows_by_lower)

    def _match_flow(industry: str) -> dict | None:
        if not flows_by_name:
            return None
        label = (industry or "").strip()
        if not label:
            return None
        # 1. Exact board name.
        if label in flows_by_name:
            return flows_by_name[label]
        # 2. Semantic alias seed (only zero-string-overlap synonyms that
        #    fuzzy/substring cannot bridge, e.g. 封测→先进封装, MLCC→陶瓷电容).
        canon = _INDUSTRY_ALIASES.get(label.lower()) or _INDUSTRY_ALIASES.get(
            _normalize_board_name(label)
        )
        if canon and canon in flows_by_name:
            return flows_by_name[canon]
        # 3. Normalized exact match (strips 概念/板块/行业 suffixes both sides).
        norm = _normalize_board_name(label)
        if norm and norm in flows_by_norm:
            return flows_by_norm[norm]
        # 4. Bidirectional substring (存储→存储芯片, CPO→CPO概念, 铜缆→铜缆高速连接).
        for name, flow in flows_by_name.items():
            if label in name or name in label:
                return flow
        # 5. difflib fuzzy fallback — self-healing bridge for typos, non-
        #    contiguous variants (光模块→光通信模块), and brand-new themes.
        return _fuzzy_match_board(label, flows_by_lower, board_names_lower)

    max_abs_inflow = max(
        (abs(f.get("main_net_inflow", 0.0)) for f in (industry_flows or [])),
        default=0.0,
    )

    max_heat = max(e["heat_score"] for e in heat.values()) or 1.0

    ranked: list[dict] = []
    unmatched_labels: set[str] = set()
    for entry in heat.values():
        flow = _match_flow(entry["industry"])
        if flow is None:
            unmatched_labels.add(entry["industry"])
        fund_flow_net = flow.get("main_net_inflow") if flow else None
        inflow_positive = (
            fund_flow_net is not None
            and fund_flow_net > max_abs_inflow * _MEANINGFUL_INFLOW_FRACTION
        )
        inflow_negative = fund_flow_net is not None and fund_flow_net < 0
        is_hot = entry["heat_score"] >= max_heat * _HOT_HEAT_FRACTION
        # Resonance classification (舆论热度 × 资金流)
        if fund_flow_net is None:
            resonance = "none"
        elif is_hot and inflow_positive:
            resonance = "strong"        # 热度资金共振
        elif is_hot and inflow_negative:
            resonance = "divergence"    # 舆论热但资金流出 — 谨慎
        elif not is_hot and inflow_positive:
            resonance = "quiet"         # 资金潜伏，热度未起
        else:
            resonance = "none"

        # Rating: A = heat + capital resonance; B = one strong signal;
        # C = fallback.
        heat_norm = entry["heat_score"] / max_heat
        if is_hot and inflow_positive:
            rating = "A"
        elif is_hot or (heat_norm >= 0.3 and fund_flow_net is not None and fund_flow_net > 0):
            rating = "B"
        else:
            rating = "C"

        ranked.append({
            "industry": entry["industry"],
            "board_name": flow.get("name", "") if flow else "",
            "industry_code": flow.get("code", "") if flow else "",
            "industry_level": flow.get("board_level", "") if flow else "",
            "heat_score": round(entry["heat_score"], 2),
            "news_count": entry["news_count"],
            "fund_flow_net": fund_flow_net,
            "change_pct": flow.get("change_pct") if flow else None,
            "resonance": resonance,
            "rating": rating,
            "top_stock_name": flow.get("top_stock_name", "") if flow else "",
            "top_stock_code": flow.get("top_stock_code", "") if flow else "",
        })

    ranked.sort(key=lambda r: r["heat_score"], reverse=True)
    for i, row in enumerate(ranked[:top_n], 1):
        row["rank"] = i
    if industry_flows and unmatched_labels:
        logger.warning(
            "Industry labels unmatched to any board (%d): %s",
            len(unmatched_labels), ", ".join(sorted(unmatched_labels)),
        )
    return ranked[:top_n]
