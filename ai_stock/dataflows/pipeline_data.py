"""Pipeline-specific data functions: impact news collection & limit-up stocks.

Separated from a_stock.py to keep the main vendor file manageable.
Imported by interface.py and registered in the signal_data category.
"""

from __future__ import annotations

import hashlib
import logging
import time as _time
import uuid
from datetime import datetime, timedelta
from typing import Annotated

import requests as _requests

from .config import get_config

logger = logging.getLogger(__name__)

# Reuse the shared User-Agent and rate-limited getter from a_stock
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)

# 新闻分页上限: 快讯密度下 4 页即可覆盖 72h 宏观窗口 (单源故障时提前终止)
_NEWS_MAX_PAGES = 4


def _em_get(url, **kwargs):
    """Thin wrapper: import the real rate-limited _em_get from a_stock at call time."""
    from .a_stock import _em_get as _real_em_get
    return _real_em_get(url, **kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_POLICY_KEYWORDS = frozenset({
    "政策", "国务院", "央行", "证监会", "银保监", "发改委", "财政部",
    "工信部", "商务部", "住建部", "部委", "地方政", "监管",
    "降准", "降息", "LPR", "MLF", "逆回购", "专项债", "国债", "补贴",
    "以旧换新", "税收优惠", "减免", "扶持", "产业规划", "指导意见",
    "通知", "办法", "条例", "强制", "禁止", "限制", "鼓励",
})


def classify_news(title: str, content: str) -> str:
    """Classify a news item as 'policy' or 'news' based on keyword matching."""
    text = (title + " " + content).lower()
    hits = sum(1 for kw in _POLICY_KEYWORDS if kw in text)
    return "policy" if hits >= 2 else "news"


def title_hash(title: str) -> str:
    """Deterministic short hash for deduplication."""
    return hashlib.md5(title.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# get_impact_news
# ---------------------------------------------------------------------------


def get_impact_news(
    curr_date: str,
    hours: int = 12,
) -> list[dict]:
    """Collect news from the past *hours* for impact assessment.

    Returns a list of dicts with keys:
        title, content, source, time, url, category, title_hash

    Sources: Eastmoney 7x24, 同花顺新闻推送, 新浪 7x24.
    Deduplication is by title hash.

    各源分页拉取 (东财 sortEnd 游标 / 新浪、同花顺 page 参数), 直到覆盖 *hours*
    窗口或页数上限; 宏观事件解析 (72h 窗口) 依赖分页, 单页仅覆盖数小时.
    单页响应解析失败即停止翻页, 保持原「单源故障不阻断」语义.

    财联社 telegraphList 子源已下线(实测 404), 于 2026-08 移除;
    百度股市通 getbannernews 子源已下线(实测返回空 HTML), 于 2026-08 替换为同花顺 + 新浪.
    """
    cutoff = datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(hours=hours)
    all_items: list[dict] = []
    seen_hashes: set[str] = set()

    # --- Source 1: Eastmoney 7x24 (sortEnd 游标分页) ---
    try:
        em_url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
        em_headers = {"User-Agent": _UA, "Referer": "https://kuaixun.eastmoney.com/"}
        sort_end = ""
        for _ in range(_NEWS_MAX_PAGES):
            em_params = {
                "client": "web", "biz": "web_724", "fastColumn": "102",
                "sortEnd": sort_end, "pageSize": "80",
                "req_trace": str(uuid.uuid4()),
            }
            r = _em_get(em_url, params=em_params, headers=em_headers, timeout=10)
            data = r.json().get("data") or {}
            items = data.get("fastNewsList", [])
            if not items:
                break
            oldest_pt = None
            for item in items:
                title = item.get("title", "")
                if not title:
                    continue
                pub_time = item.get("showTime", "")
                pt = None
                if pub_time:
                    try:
                        pt = datetime.strptime(pub_time[:16], "%Y-%m-%d %H:%M")
                    except ValueError:
                        pt = None
                if pt is not None:
                    if oldest_pt is None or pt < oldest_pt:
                        oldest_pt = pt
                    if pt < cutoff:
                        continue
                h = title_hash(title)
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)
                all_items.append({
                    "title": title,
                    "content": item.get("summary", "")[:300],
                    "source": "东方财富",
                    "time": pub_time,
                    "url": "",
                    "category": classify_news(title, item.get("summary", "")),
                    "title_hash": h,
                })
            next_cursor = str(data.get("sortEnd") or "")
            # 游标无推进 (防死循环) 或已翻过窗口起点 → 停止
            if not next_cursor or next_cursor == sort_end:
                break
            sort_end = next_cursor
            if oldest_pt is not None and oldest_pt < cutoff:
                break
    except Exception as e:
        logger.warning("Eastmoney impact news fetch failed: %s", e)

    # --- Source 2: 同花顺新闻推送 (替代已下线的百度股市通, page 分页) ---
    try:
        ths_url = "https://news.10jqka.com.cn/tapp/news/push/stock/"
        for page in range(1, _NEWS_MAX_PAGES + 1):
            ths_params = {
                "page": str(page), "tag": "", "track": "website", "pagesize": "50",
            }
            r = _requests.get(
                ths_url, params=ths_params, headers={"User-Agent": _UA}, timeout=10,
            )
            items = r.json().get("data", {}).get("list", []) or []
            if not items:
                break
            oldest_pt = None
            for item in items:
                title = item.get("title", "")
                if not title:
                    continue
                content = item.get("digest", "")[:300]
                pt = None
                try:
                    pt = datetime.fromtimestamp(int(item.get("ctime", 0)))
                except (TypeError, ValueError):
                    pt = None
                pub_time = pt.strftime("%Y-%m-%d %H:%M") if pt else ""
                if pt is not None:
                    if oldest_pt is None or pt < oldest_pt:
                        oldest_pt = pt
                    if pt < cutoff:
                        continue
                h = title_hash(title)
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)
                all_items.append({
                    "title": title,
                    "content": content,
                    "source": "同花顺",
                    "time": pub_time,
                    "url": item.get("url", ""),
                    "category": classify_news(title, content),
                    "title_hash": h,
                })
            if oldest_pt is not None and oldest_pt < cutoff:
                break
    except Exception as e:
        logger.warning("THS impact news fetch failed: %s", e)

    # --- Source 3: 新浪 7x24 快讯 (page 分页) ---
    try:
        sina_url = "https://zhibo.sina.com.cn/api/zhibo/feed"
        sina_headers = {"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/7x24/"}
        for page in range(1, _NEWS_MAX_PAGES + 1):
            sina_params = {
                "page": str(page), "page_size": "50", "zhibo_id": "152", "tag_id": "0",
            }
            r = _requests.get(sina_url, params=sina_params, headers=sina_headers, timeout=10)
            items = r.json().get("result", {}).get("data", {}).get("feed", {}).get("list", []) or []
            if not items:
                break
            oldest_pt = None
            for item in items:
                rich_text = (item.get("rich_text") or "").strip()
                if not rich_text:
                    continue
                title = rich_text[:120]
                pub_time = item.get("create_time", "")
                if pub_time:
                    try:
                        pt = datetime.strptime(pub_time[:16], "%Y-%m-%d %H:%M")
                        if oldest_pt is None or pt < oldest_pt:
                            oldest_pt = pt
                        if pt < cutoff:
                            continue
                    except ValueError:
                        pass
                h = title_hash(title)
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)
                all_items.append({
                    "title": title,
                    "content": rich_text[:300],
                    "source": "新浪财经",
                    "time": pub_time,
                    "url": item.get("docurl", ""),
                    "category": classify_news(title, rich_text),
                    "title_hash": h,
                })
            if oldest_pt is not None and oldest_pt < cutoff:
                break
    except Exception as e:
        logger.warning("Sina impact news fetch failed: %s", e)

    logger.info(
        "Collected %d unique news items for %s (past %dh)",
        len(all_items), curr_date, hours,
    )
    return all_items


# ---------------------------------------------------------------------------
# get_limit_up_stocks
# ---------------------------------------------------------------------------


def get_limit_up_stocks(
    curr_date: str,
    days: int = 7,
) -> list[dict]:
    """Get stocks that hit limit-up in the past *days* trading days.

    Returns a list of dicts with keys:
        code, name, date, reason_tags, zhangfu, huanshou, chengjiaoe, dde

    Reuses the 同花顺 hot-stocks API for each day.
    """
    all_entries: list[dict] = []
    seen: set[str] = set()  # (code, date) dedup

    base_date = datetime.strptime(curr_date, "%Y-%m-%d")
    checked = 0
    calendar_days = 0

    while checked < days and calendar_days < 30:
        dt = base_date - timedelta(days=calendar_days)
        calendar_days += 1
        # Skip weekends
        if dt.weekday() >= 5:
            continue
        date_str = dt.strftime("%Y-%m-%d")
        checked += 1

        try:
            url = (
                f"http://zx.10jqka.com.cn/event/api/getharden/"
                f"date/{date_str}/orderby/date/orderway/desc/charset/GBK/"
            )
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "Chrome/117.0.0.0 Safari/537.36"
                ),
            }
            r = _requests.get(url, headers=headers, timeout=10)
            data = r.json()
            if data.get("errocode", 0) != 0:
                continue
            for row in data.get("data") or []:
                code = row.get("code", "")
                key = f"{code}_{date_str}"
                if key in seen:
                    continue
                seen.add(key)
                reason = row.get("reason", "")
                tags = [t.strip() for t in reason.split("+") if t.strip()] if reason else []
                all_entries.append({
                    "code": code,
                    "name": row.get("name", ""),
                    "date": date_str,
                    "reason_tags": tags,
                    "zhangfu": row.get("zhangfu", ""),
                    "huanshou": row.get("huanshou", ""),
                    "chengjiaoe": row.get("chengjiaoe", ""),
                    "dde": row.get("ddejingliang", ""),
                })
            # Rate limit: >=0.5s between API calls
            _time.sleep(0.5)
        except Exception as e:
            logger.warning("Limit-up fetch failed for %s: %s", date_str, e)

    logger.info(
        "Collected %d limit-up entries over %d trading days ending %s",
        len(all_entries), days, curr_date,
    )
    return all_entries


# ---------------------------------------------------------------------------
# Industry board data (行业板块行情 + 主力资金净流入)
# ---------------------------------------------------------------------------

# 东财 push2 字段映射：
#   f12 板块代码(BKxxxx) / f14 名称 / f3 涨跌幅% / f62 主力净流入额(元)
#   f104 上涨家数 / f105 下跌家数 / f128 领涨股名 / f140 领涨股代码 / f136 领涨股涨跌幅%
_EM_INDUSTRY_FIELDS = "f12,f14,f3,f62,f104,f105,f128,f136,f140"

# 部分网络环境（公司代理/区域风控）会直接断连 push2 主域及其数字镜像，
# 但延迟行情域 push2delay 始终可用。行业资金流是分钟级数据，延迟域
# 完全够用；按序尝试，首个可达主机生效。
_EM_PUSH2_BASES = (
    "https://push2.eastmoney.com",
    "https://push2delay.eastmoney.com",
)
# 与 a_stock._push2_get 共用同一套粘性/死主机状态 (_push2_pick 读全局单例):
# 主域被环境性封禁时不再每请求白吃一次断连, 任一调用方探测到可达主机后,
# 另一调用方直接沿用, 不重复探死主机.
_EM_BOARD_HEADERS = {
    "User-Agent": _UA,
    "Referer": "https://quote.eastmoney.com/",
}


def _push2_get(path: str, params: dict):
    """GET a push2 API path with sticky host fallback (main ↔ delay mirror)."""
    from .a_stock import _mark_dead_on_conn_error, _push2_pick

    last_exc: Exception | None = None
    for idx in _push2_pick(_EM_PUSH2_BASES):
        base = _EM_PUSH2_BASES[idx]
        full_url = f"{base}{path}"
        try:
            resp = _em_get(
                full_url, params=params,
                headers=_EM_BOARD_HEADERS, timeout=15,
            )
        except Exception as exc:
            last_exc = exc
            _mark_dead_on_conn_error(exc, full_url)
            continue
        _mark_push2_last_ok(idx)
        return resp
    raise last_exc


def _mark_push2_last_ok(idx: int) -> None:
    """把可达主机下标写回 a_stock 的全局粘性状态 (两模块共用一份)."""
    from .a_stock import _EM_PUSH2_LAST_OK
    _EM_PUSH2_LAST_OK[0] = idx


# 东财 push2 板块口径：
#   m:90+t:2  行业板块（数百个，细分到 半导体材料/半导体设备/种子/种植业…）
#   m:90+t:3  概念板块（数百个，细分到 MLCC/先进封装/存储芯片/CPO…）
# 行业榜要落到 MLCC、半导体封测等细分级别，须同时拉取行业 + 概念板块资金流。
_EM_BOARD_FS = {
    "industry": "m:90+t:2",
    "concept": "m:90+t:3",
}


def _fetch_board_flow(board_level: str) -> list[dict]:
    """Fetch Eastmoney board fund-flow for one board taxonomy.

    Args:
        board_level: "industry" (行业板块) or "concept" (概念板块).

    Returns a list of dicts with keys:
        code (BKxxxx), name, change_pct, main_net_inflow (元),
        up_count, down_count, top_stock_name, top_stock_code, top_stock_pct,
        board_level ("industry" / "concept").

    Raises the underlying exception when the vendor host refuses the connection so
    that the caller can fall back to another provider (see ``_fetch_board_flow_any``).
    """
    fs = _EM_BOARD_FS[board_level]
    boards: list[dict] = []
    page = 1
    while page <= 10:  # pz is capped at ~100/页; paginate until empty
        params = {
            "pn": str(page),
            "pz": "100",
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": fs,
            "fields": _EM_INDUSTRY_FIELDS,
        }
        r = _push2_get("/api/qt/clist/get", params)
        items = r.json().get("data", {}).get("diff", []) or []
        if not items:
            break
        for item in items:
            boards.append({
                "code": str(item.get("f12", "")),
                "name": str(item.get("f14", "")),
                "change_pct": _to_float(item.get("f3")),
                "main_net_inflow": _to_float(item.get("f62")),
                "up_count": int(_to_float(item.get("f104"))),
                "down_count": int(_to_float(item.get("f105"))),
                "top_stock_name": str(item.get("f128", "")),
                "top_stock_code": str(item.get("f140", "")),
                "top_stock_pct": _to_float(item.get("f136")),
                "board_level": board_level,
                "source": "eastmoney_push2",
            })
        if len(items) < 100:
            break
        page += 1
    return boards


# ---------------------------------------------------------------------------
# 备用数据源: 新浪板块行情 (P0-1)
# ---------------------------------------------------------------------------
# 东财 push2 的 clist 集群会对采集机 IP 做应用层封禁 (TCP/TLS 均正常, 仅该接口
# 被断连), 且主域与 delay 镜像同时失效 → 原有「主域失败回退延迟域」机制无效。
# 新浪的两个 JS 接口同口径返回板块名/涨跌幅/成交额, 且不走东财反爬体系:
#   行业:  q/view/newSinaHy.php            → "new_xxx":"code,name,count,..."
#   概念:  q/view/newFLJK.php?param=class  → "gn_xxx":"code,name,count,..."
# 字段 (逗号分隔, GBK 编码):
#   0=板块代码 1=板块名 2=成分数 3=均价 4=涨跌额 5=涨跌幅% 6=成交量 7=成交额
#   8=领涨股代码 9..11=价格字段 12=领涨股名
# 语义差异: 无主力净流入 (置 0)、无成分股涨跌家数 (置 0) —— 下游按「可得字段」
# 使用, 排序退化为纯涨幅榜, 但选股链路不再空转。
_SINA_BOARD_URLS = {
    "industry": "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php",
    "concept": (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.getHQNodeData"
    ),
}
_SINA_CLASS_BOARD_URL = (
    "https://vip.stock.finance.sina.com.cn/q/view/newFLJK.php?param=class"
)
_SINA_NODE_DATA_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
_SINA_HEADERS = {
    "User-Agent": _UA,
    "Referer": "https://finance.sina.com.cn/",
    "Accept": "*/*",
}


def _sina_board_flow(board_level: str) -> list[dict]:
    """新浪板块行情 (行业 / 概念), 与东财口径对齐的板flow 结构."""
    import json as _json
    import re as _re

    url = (
        _SINA_CLASS_BOARD_URL if board_level == "concept"
        else _SINA_BOARD_URLS[board_level]
    )
    resp = _requests.get(url, headers=_SINA_HEADERS, timeout=12)
    resp.raise_for_status()
    # 接口返回 GBK 编码的 JS 变量赋值, 显式 decode 避免 requests 误判 latin-1
    text = resp.content.decode("gbk", errors="ignore")
    match = _re.search(r"=\s*(\{.*\})\s*;?\s*$", text.strip(), _re.S)
    if not match:
        raise ValueError("sina board payload unexpected")
    payload = _json.loads(match.group(1))

    boards: list[dict] = []
    for raw in payload.values():
        parts = str(raw).split(",")
        if len(parts) < 8:
            continue
        code = parts[0].strip()
        name = parts[1].strip()
        if not code or not name:
            continue
        boards.append({
            "code": code,
            "name": name,
            "change_pct": _to_float(parts[5]),
            "main_net_inflow": 0.0,          # 新浪不提供主力净流入
            "up_count": 0,
            "down_count": 0,
            "top_stock_name": parts[12].strip() if len(parts) > 12 else "",
            "top_stock_code": _strip_sina_prefix(parts[8]) if len(parts) > 8 else "",
            "top_stock_pct": 0.0,
            "board_level": board_level,
            "source": "sina_board",
        })
    return boards


def _strip_sina_prefix(symbol: str) -> str:
    """'sh600629' -> '600629' (与东财 f12 口径一致, 便于下游按 6 位代码匹配)."""
    value = str(symbol or "").strip().lower()
    for prefix in ("sh", "sz", "bj"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def _sina_board_constituents(
    board_code: str, top_n: int = 20, sort_by: str = "amount",
) -> list[dict]:
    """新浪板块成分股 (行业/概念节点), 结构对齐 ``get_board_constituents``."""
    sort_key = "mktcap" if sort_by == "market_cap" else "amount"
    params = {
        "page": "1",
        "num": str(max(top_n, 1)),
        "sort": sort_key,
        "asc": "0",
        "node": board_code,
    }
    resp = _requests.get(
        _SINA_NODE_DATA_URL, params=params, headers=_SINA_HEADERS, timeout=12,
    )
    resp.raise_for_status()
    items = resp.json() or []
    stocks: list[dict] = []
    for item in items:
        code = str(item.get("code", "") or "")
        if not code:
            continue
        # 新浪 mktcap/nmc 单位为万元, 东财 f20 为元 → 统一换算成元
        stocks.append({
            "code": code,
            "name": str(item.get("name", "")),
            "price": _to_float(item.get("trade")),
            "change_pct": _to_float(item.get("changepercent")),
            "amount": _to_float(item.get("amount")),
            "turnover_rate": _to_float(item.get("turnoverratio")),
            # 新浪无「量比」字段; 置 0 表示不可用 (雷达按 >2 过滤, 自然不触发)
            "volume_ratio": 0.0,
            "market_cap": _to_float(item.get("mktcap")) * 1e4,
            "main_net_inflow": 0.0,
            "source": "sina_board",
        })
    return stocks


def _fetch_board_flow_any(board_level: str) -> list[dict]:
    """板块列表多源取数: 东财 push2 (主) → 新浪 (备).

    每个 provider 独立记录健康度与退避: 主源在退避窗口内直接跳过, 避免
    「被封期间每 10 分钟硬撞一次」把封禁越撞越久 (P0-1)。
    """
    from . import data_health

    providers = (
        ("eastmoney_push2", "board_list", lambda: _fetch_board_flow(board_level)),
        ("sina_board", "board_list", lambda: _sina_board_flow(board_level)),
    )
    last_error = ""
    for source, api, fn in providers:
        if data_health.in_backoff(source):
            logger.info(
                "%s in backoff for %s, skip provider", source, board_level,
            )
            continue
        started = _time.monotonic()
        try:
            rows = fn()
        except Exception as exc:
            last_error = f"{source}: {exc}"
            in_backoff = data_health.record_fail(
                source, api, str(exc),
                backoff_steps=_board_backoff_steps(), threshold=_board_fail_threshold(),
            )
            logger.warning(
                "%s board_list(%s) failed: %s%s",
                source, board_level, exc, " (entering backoff)" if in_backoff else "",
            )
            continue
        if not rows:
            # 空结果按失败处理: 接口「成功但返回 0 条」是最危险的静默故障形态
            data_health.record_fail(
                source, api, "empty_result",
                backoff_steps=_board_backoff_steps(), threshold=_board_fail_threshold(),
            )
            logger.warning("%s board_list(%s) returned 0 rows", source, board_level)
            continue
        # 统一标注实际供数来源: 下游排查「数据是哪个源给的」时无需再看日志
        for row in rows:
            if isinstance(row, dict):
                row.setdefault("source", source)
        data_health.record_ok(
            source, api, (_time.monotonic() - started) * 1000.0,
        )
        if source != "eastmoney_push2":
            logger.info(
                "board_list(%s) served by %s (%d rows) — primary source unavailable",
                board_level, source, len(rows),
            )
        return rows
    if last_error:
        logger.warning("all board providers failed for %s: %s", board_level, last_error)
    return []


def _board_backoff_steps() -> tuple[int, ...]:
    try:
        from ai_stock.quant.config import DATA_SOURCE_BACKOFF_STEPS

        return tuple(DATA_SOURCE_BACKOFF_STEPS)
    except Exception:  # pragma: no cover - quant 未安装时用保守默认
        return (300, 900, 3600)


def _board_fail_threshold() -> int:
    try:
        from ai_stock.quant.config import DATA_SOURCE_FAIL_THRESHOLD

        return int(DATA_SOURCE_FAIL_THRESHOLD)
    except Exception:  # pragma: no cover
        return 3


def get_industry_fund_flow() -> list[dict]:
    """Fetch all 行业板块 (m:90+t:2, ~500 boards) with realtime price change
    and main capital net inflow (主力净流入).

    Backward-compatible entry point. For fine-grained (概念板块级别) matching
    use ``get_concept_board_flow`` or ``get_all_board_fund_flow``.

    Empty list on failure — the ranking degrades to news-heat-only.
    """
    try:
        boards = _fetch_board_flow_any("industry")
    except Exception as e:
        logger.warning("Industry fund-flow fetch failed: %s", e)
        return []
    logger.info("Fetched %d industry boards with fund flow", len(boards))
    return boards


def get_concept_board_flow() -> list[dict]:
    """Fetch all 概念板块 (m:90+t:3, ~500 boards) with realtime price change
    and main capital net inflow — the fine-grained MLCC/先进封装/存储芯片 level.

    Empty list on failure.
    """
    boards = _fetch_board_flow_any("concept")
    logger.info("Fetched %d concept boards with fund flow", len(boards))
    return boards


def get_all_board_fund_flow() -> list[dict]:
    """Fetch both 行业板块 + 概念板块 fund flow (merged, tagged by board_level).

    This is the universe the industry heatmap matches against: concept boards
    give the fine granularity, industry boards give the standard classification.

    多源容错 (P0-1): 每个板块类别独立走「东财 push2 → 新浪」链路, 单源被封不会
    让整张榜单归零; 两源都不可用时才返回空 (由调用方走缓存兜底)。
    """
    boards: list[dict] = []
    for level in ("industry", "concept"):
        boards.extend(_fetch_board_flow_any(level))
    if boards:
        sources = sorted({b.get("source", "?") for b in boards})
        logger.info(
            "Fetched %d total boards (industry + concept), source=%s",
            len(boards), ",".join(sources),
        )
    else:
        logger.warning("All board providers returned empty (industry + concept)")
    return boards


# 成分股口径：按成交额/市值取流动性较好的个股 (选股候选池)
_CONSTITUENT_FIELDS = "f12,f14,f2,f3,f6,f8,f10,f20,f62"
_CONSTITUENT_SORT_FIELDS = {"amount": "f6", "market_cap": "f20"}


def get_board_constituents(
    board_code: str,
    top_n: int = 20,
    sort_by: str = "amount",
) -> list[dict]:
    """Fetch a board's constituent stocks sorted by liquidity or market cap.

    Args:
        board_code: Eastmoney board code, e.g. "BK1033".
        top_n: number of stocks to return.
        sort_by: "amount" (成交额, 流动性口径) or "market_cap" (总市值).

    Returns a list of dicts with keys:
        code, name, price, change_pct, turnover_rate (%), volume_ratio,
        market_cap (元), amount (成交额, 元), main_net_inflow (元).

    Empty list on failure.

    数据源: 东财 push2 (主, 板块代码形如 ``BK1033``) → 新浪节点 (备, 板块代码形如
    ``new_dlhy`` / ``gn_hwqc``)。新浪来源的板块代码无法转换到东财, 因此备用板块
    列表天然只能由备用成分股接口承接, 两条链路保持一致 (P0-1)。
    """
    if not board_code:
        return []
    from . import data_health

    if board_code.startswith(("new_", "gn_")):
        return _sina_constituents_guarded(board_code, top_n, sort_by)

    if data_health.in_backoff("eastmoney_push2"):
        # 主源在退避窗口内: BKxxxx 系东财板块代码, 无备用源可承接 → 直接空
        logger.info("eastmoney in backoff, skip constituents for %s", board_code)
        return []

    started = _time.monotonic()
    try:
        params = {
            "pn": "1",
            "pz": str(max(top_n, 1)),
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": _CONSTITUENT_SORT_FIELDS.get(sort_by, "f6"),
            "fs": f"b:{board_code}",
            "fields": _CONSTITUENT_FIELDS,
        }
        r = _push2_get("/api/qt/clist/get", params)
        items = r.json().get("data", {}).get("diff", []) or []
        stocks: list[dict] = []
        for item in items:
            code = str(item.get("f12", ""))
            if not code:
                continue
            stocks.append({
                "code": code,
                "name": str(item.get("f14", "")),
                "price": _to_float(item.get("f2")),
                "change_pct": _to_float(item.get("f3")),
                "amount": _to_float(item.get("f6")),
                "turnover_rate": _to_float(item.get("f8")),
                "volume_ratio": _to_float(item.get("f10")),
                "market_cap": _to_float(item.get("f20")),
                "main_net_inflow": _to_float(item.get("f62")),
                "source": "eastmoney_push2",
            })
        if stocks:
            data_health.record_ok(
                "eastmoney_push2", "board_constituents",
                (_time.monotonic() - started) * 1000.0,
            )
        else:
            data_health.record_fail(
                "eastmoney_push2", "board_constituents", "empty_result",
                backoff_steps=_board_backoff_steps(), threshold=_board_fail_threshold(),
            )
        return stocks
    except Exception as e:
        data_health.record_fail(
            "eastmoney_push2", "board_constituents", str(e),
            backoff_steps=_board_backoff_steps(), threshold=_board_fail_threshold(),
        )
        logger.warning("Board constituents fetch failed for %s: %s", board_code, e)
        return []


def _sina_constituents_guarded(
    board_code: str, top_n: int, sort_by: str,
) -> list[dict]:
    """新浪成分股取数 + 健康度记录 (备用链路专用)."""
    from . import data_health

    started = _time.monotonic()
    try:
        stocks = _sina_board_constituents(board_code, top_n=top_n, sort_by=sort_by)
    except Exception as e:
        data_health.record_fail(
            "sina_board", "board_constituents", str(e),
            backoff_steps=_board_backoff_steps(), threshold=_board_fail_threshold(),
        )
        logger.warning("Sina constituents failed for %s: %s", board_code, e)
        return []
    if stocks:
        data_health.record_ok(
            "sina_board", "board_constituents",
            (_time.monotonic() - started) * 1000.0,
        )
    else:
        data_health.record_fail(
            "sina_board", "board_constituents", "empty_result",
            backoff_steps=_board_backoff_steps(), threshold=_board_fail_threshold(),
        )
    return stocks


def _to_float(value) -> float:
    """Best-effort float conversion; Eastmoney returns '-' for missing values."""
    try:
        if value in (None, "", "-"):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
