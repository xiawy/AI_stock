"""证据链: 给定一个主题, 一次返回三榜各自持有的证据。

为什么需要这个接口: 新闻榜说"CPO 利好"、行业榜把 CPO 排第 7、热股榜里有 3 只
CPO 票, 但三个页面互不引用。用户想确认"这是不是同一件事", 只能在三个标签页
之间来回找。这里把四段(新闻 → 行业 → 龙头 → 池内标的)一次拼齐, 前端只渲染
不加工, 三榜复用同一个组件。

命名匹配统一走 ``strategy.rules.textmatch`` —— 全站只留一份"这两个名字是不是
同一件事"的实现, 否则第四次出现同一主题被三处判成三个不同板块。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 池内标的的行业名与主题名往往不是字面一致(「锂电池」vs「锂电」), 靠 textmatch
# 的包含+核心词降级处理; 这里只限制返回条数, 避免弹窗被几十行灌满。
MAX_POOL_ROWS = 20


def build_chain(theme: str, *, date_str: str = "") -> dict:
    """组装 ``{theme, news[], industry, leaders[], pool[], notes[]}``。

    任一环节缺数据时返回空列表 + 一条 ``notes`` 说明, **不编造**。
    """
    theme = (theme or "").strip()
    if not theme:
        return {"theme": "", "news": [], "industry": None,
                "leaders": [], "pool": [], "notes": ["未指定主题"]}

    from ai_stock.quant.strategy.rules.textmatch import match

    notes: list[str] = []
    news = _news_for_theme(theme, date_str, match)
    if not news:
        notes.append("新闻榜当前快照没有该主题的条目")

    industry, leaders = _industry_for_theme(theme, date_str, match)
    if industry is None:
        notes.append("行业榜没有该主题（或名称差异过大）")

    pool = _pool_for_theme(theme, industry, match)
    if not pool:
        notes.append("自选池没有该主题的标的")

    return {
        "theme": theme,
        "news": news,
        "industry": industry,
        "leaders": leaders,
        "pool": pool,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 三段取数
# ---------------------------------------------------------------------------


def _news_for_theme(theme: str, date_str: str, match) -> list[dict]:
    from app.services.pipeline_service import get_pipeline_service

    svc = get_pipeline_service()
    payload = svc.get_by_date(date_str) if date_str else svc.get_latest()
    items = (payload or {}).get("news_items") or []
    out: list[dict] = []
    for item in items:
        if _news_hits(item, theme, match):
            out.append(_slim_news(item))
    return out


def _news_hits(item: dict, theme: str, match) -> bool:
    """一条新闻是否属于该主题。

    只看**已结构化的主题字段**(industry_impacts / industries), 不看标题命中 ——
    标题里出现"电力"不等于这条新闻在讲电力板块, 用标题命中会把回归分析做假。
    """
    for impact in item.get("industry_impacts") or []:
        if match(theme, str((impact or {}).get("name") or "")):
            return True
    for name in item.get("industries") or []:
        if match(theme, str(name or "")):
            return True
    return False


def _slim_news(item: dict) -> dict:
    impacts = [
        {
            "name": i.get("name", ""),
            "direction": i.get("direction", ""),
            "strength": i.get("strength", 0),
            "expected_gain": i.get("expected_gain", ""),
        }
        for i in item.get("industry_impacts") or []
        if (i or {}).get("name")
    ]
    return {
        "id": item.get("id"),
        "title": item.get("title", ""),
        "source": item.get("source", ""),
        "pub_time": item.get("pub_time", ""),
        "event_type": item.get("event_type", ""),
        "importance": item.get("importance", 0),
        "bias_scope": item.get("bias_scope", ""),
        "bull_bear_bias": item.get("bull_bear_bias", ""),
        "confidence": item.get("confidence", 0),
        "assess_source": item.get("assess_source", ""),
        "cluster_id": item.get("cluster_id", ""),
        "source_count": item.get("source_count", 1),
        "impacts": impacts,
    }


def _industry_for_theme(theme: str, date_str: str, match) -> tuple[dict | None, list[dict]]:
    from app.services.pipeline_service import get_pipeline_service

    svc = get_pipeline_service()
    payload = (
        svc.get_industry_by_date(date_str) if date_str else svc.get_industry_latest()
    ) or {}
    rows = payload.get("rankings") or []
    hit = next(
        (r for r in rows if match(theme, str(r.get("industry") or ""))),
        None,
    )
    if hit is None:
        return None, []
    return (
        {
            "industry": hit.get("industry", ""),
            "rank": hit.get("rank"),
            "heat_score": hit.get("heat_score"),
            "stage": hit.get("stage", ""),
            "change_pct": hit.get("change_pct"),
            "main_net_inflow": hit.get("main_net_inflow"),
            "event_tag": hit.get("event_tag", ""),
            "transmission_from": hit.get("transmission_from", ""),
        },
        list(hit.get("leader_stocks") or []),
    )


def _pool_for_theme(theme: str, industry: dict | None, match) -> list[dict]:
    from ai_stock.quant.db_ops import get_optional_pool

    from .board_view import annotate_pool

    industry_name = (industry or {}).get("industry") or ""
    rows: list[dict] = []
    for stock in annotate_pool(get_optional_pool("active")):
        if match(theme, str(stock.get("radar_theme") or "")) or (
            industry_name and match(industry_name, str(stock.get("industry") or ""))
        ):
            rows.append(_slim_pool(stock))
        if len(rows) >= MAX_POOL_ROWS:
            break
    return rows


def _slim_pool(stock: dict) -> dict:
    return {
        "symbol": stock.get("symbol", ""),
        "name": stock.get("name", ""),
        "industry": stock.get("industry", ""),
        "confidence": stock.get("confidence"),
        "dyn_confidence": stock.get("dyn_confidence"),
        "dyn_state": stock.get("dyn_state", ""),
        "stage_batch": stock.get("stage_batch", ""),
        "is_tail": stock.get("is_tail", False),
        "radar_theme": stock.get("radar_theme", ""),
    }
