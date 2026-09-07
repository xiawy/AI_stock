"""Structured data service for quant agents (设计文档 §9.1).

取数统一走共享工具层 ``ai_stock.tools`` (fetch_* 纯函数内核), 本层负责适配成结构化数据,
统一经过熔断器 + 三级缓存:
- 涨停股 / 新闻 / 基本面 / 解禁 / 增减持 / 盈利预测 / 概念板块 — ai_stock.tools 内核
- 板块资金流 / 板块成分股 — 直接复用 pipeline_data 的结构化函数
- K 线 — 解析 get_stock_data 的 CSV 文本 → DataFrame → stockstats 本地算指标
- 实时行情 — 东财 push2 单股接口 (复用 a_stock 的节流 _em_get)
- 外部接口返回 None/异常时返回缓存兜底并打告警日志, 不直接上抛策略层.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from io import StringIO
from typing import Optional

from .cache_manager import get_breaker, get_cache_manager
from .config import (
    CACHE_TTL,
    RADAR_CHANGE_PCT_MAX,
    RADAR_CHANGE_PCT_MIN,
    RADAR_MARKET_CAP_MAX,
    RADAR_MAX_THEMES,
    RADAR_MIN_CLUSTER,
    RADAR_VOLUME_RATIO_MIN,
)

logger = logging.getLogger(__name__)

# A 股市场时区。拼装"当前日期"必须按市场所在地算, 不能用主机本地时区——
# 主机时区偏东/偏西会让本地日期超前/落后市场当天, 导致未来函数告警误报、
# 涨停/新闻/解禁等按日期取数错过或错取交易日 (与 a_stock._market_today 同理).
_MARKET_TZ = timezone(timedelta(hours=8))


def _market_today() -> str:
    """A 股市场当前日期 (YYYY-MM-DD), 与主机时区无关."""
    return datetime.now(_MARKET_TZ).strftime("%Y-%m-%d")


class DataService:
    """量化 Agent 共用的结构化数据门面 (单例见 get_data_service)."""

    def __init__(self):
        self.cache = get_cache_manager()

    # ------------------------------------------------------------------
    # 板块 / 行业
    # ------------------------------------------------------------------

    def get_all_board_fund_flow(self) -> list[dict]:
        """行业+概念板块行情与主力净流入 (结构化: code/name/change_pct/main_net_inflow...)."""
        def _fetch() -> list[dict]:
            from ai_stock.dataflows.pipeline_data import get_all_board_fund_flow

            return get_all_board_fund_flow()

        return self.cache.get_or_fetch(
            "board_fund_flow",
            _fetch,
            ttl=CACHE_TTL["fund_flow"],
            category="fund_flow",
            breaker=get_breaker("board_fund_flow"),
            allow_fallback_stale=True,
        ) or []

    def get_top_industries(self, top_n: int = 5) -> list[dict]:
        """涨幅前 N ∪ 主力净流入前 N 的候选行业 (并集, 去重)."""
        boards = self.get_all_board_fund_flow()
        if not boards:
            return []
        by_change = sorted(boards, key=lambda b: b.get("change_pct", 0), reverse=True)
        by_inflow = sorted(
            boards, key=lambda b: b.get("main_net_inflow", 0), reverse=True,
        )
        merged: list[dict] = []
        seen: set[str] = set()
        for board in by_change[:top_n] + by_inflow[:top_n]:
            code = board.get("code", "")
            if code and code not in seen:
                seen.add(code)
                merged.append(board)
        return merged

    def get_limit_up_stocks(self, days: int = 1) -> list[dict]:
        """近 N 个交易日涨停股 (含 reason_tags 行业归因)."""
        def _fetch() -> list[dict]:
            from ai_stock.tools import fetch_limit_up_stocks

            return fetch_limit_up_stocks(
                _market_today(), days=days,
            )

        return self.cache.get_or_fetch(
            f"limit_up_{days}d",
            _fetch,
            ttl=CACHE_TTL["fund_flow"],
            category="fund_flow",
            breaker=get_breaker("limit_up"),
            allow_fallback_stale=True,
        ) or []

    def get_all_industries(self) -> list[dict]:
        """东财全部分类板块列表 (行业 + 概念, 含当日量价与主力资金)."""
        return self.get_all_board_fund_flow()

    def get_industry_detail(self, industry_code: str) -> Optional[dict]:
        """单个板块的当日涨跌幅/主力净流入/领涨股等 (取自全板块资金流)."""
        if not industry_code:
            return None
        for board in self.get_all_board_fund_flow():
            if board.get("code") == industry_code:
                return board
        return None

    def get_industry_stocks(
        self,
        industry_code: str,
        top_n: int = 20,
        sort_by: str = "amount",
    ) -> list[dict]:
        """板块成分股 (按成交额/市值降序, 取流动性较好的前 top_n 只)."""
        if not industry_code:
            return []

        def _fetch() -> list[dict]:
            from ai_stock.dataflows.pipeline_data import get_board_constituents

            return get_board_constituents(industry_code, top_n=top_n, sort_by=sort_by)

        return (self.cache.get_or_fetch(
            f"board_constituents:{industry_code}:{sort_by}:{top_n}",
            _fetch,
            ttl=CACHE_TTL["fund_flow"],
            category="fund_flow",
            breaker=get_breaker("board_constituents"),
            allow_fallback_stale=True,
        ) or [])[:top_n]

    # ------------------------------------------------------------------
    # 个股 K 线 + 技术指标 (本地 stockstats 计算)
    # ------------------------------------------------------------------

    def get_ohlcv(self, symbol: str, lookback_days: int = 120) -> "object":
        """个股日 K DataFrame (Date/Open/High/Low/Close/Volume), 解析自 get_stock_data."""
        import pandas as pd

        end = _market_today()
        start = (datetime.now(_MARKET_TZ) - timedelta(days=int(lookback_days * 1.7) + 15)).strftime(
            "%Y-%m-%d",
        )

        def _fetch():
            from ai_stock.dataflows.a_stock import get_stock_data

            text = get_stock_data(symbol, start, end)
            if not isinstance(text, str) or "Date" not in text:
                raise ValueError(f"K线数据不可用: {symbol}")
            csv_lines = [
                line for line in text.splitlines() if line and not line.startswith("#")
            ]
            df = pd.read_csv(StringIO("\n".join(csv_lines)))
            df["Date"] = pd.to_datetime(df["Date"])
            for col in ("Open", "High", "Low", "Close"):
                df[col] = df[col].astype(float)
            df["Volume"] = df["Volume"].astype(float)
            df = df.tail(lookback_days).reset_index(drop=True)
            # 转 JSON 可序列化结构再入缓存 (DataFrame 直接存会被
            # json.dumps(default=str) 压成字符串, 跨进程/重启后读回损坏)
            df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
            return df.to_dict(orient="list")

        cached = self.cache.get_or_fetch(
            f"ohlcv:{symbol}:{lookback_days}",
            _fetch,
            ttl=CACHE_TTL["ohlcv"],
            category="ohlcv",
            breaker=get_breaker("ohlcv"),
            allow_fallback_stale=True,
        )
        if cached is None:
            return None
        try:
            import pandas as pd

            if not isinstance(cached, dict):
                # 历史脏数据 (旧版 DataFrame 被压成字符串), 丢弃待下次回源刷新
                logger.warning("ohlcv cache payload not a dict for %s, refetch later", symbol)
                return None
            df = pd.DataFrame(cached)
            df["Date"] = pd.to_datetime(df["Date"])
            for col in ("Open", "High", "Low", "Close", "Volume"):
                df[col] = df[col].astype(float)
            return df
        except Exception as exc:
            logger.warning("ohlcv cache decode failed for %s: %s", symbol, exc)
            return None

    def get_stock_indicators(
        self,
        symbol: str,
        lookback_days: int = 120,
    ) -> Optional[dict]:
        """基于日 K 本地计算技术指标 (stockstats), 返回最近一根 K 线的指标 dict.

        指标: boll(中轨)/boll_ub/boll_lb, macd/macds/macdh, rsi, kdjk/kdjd/kdj,
        atr, close_10_ema, close_50_sma, close_200_sma, volume 比率.
        """
        df = self.get_ohlcv(symbol, lookback_days)
        if df is None or len(df) < 30:
            return None
        try:
            return compute_indicators(df)
        except Exception as exc:
            logger.warning("Indicator compute failed for %s: %s", symbol, exc)
            return None

    def get_recent_indicator_series(
        self,
        symbol: str,
        indicator: str,
        count: int = 10,
        lookback_days: int = 120,
    ) -> list[tuple[str, float]]:
        """返回某指标最近 count 个交易日的 (date, value) 序列 (信号确认用)."""
        df = self.get_ohlcv(symbol, lookback_days)
        if df is None or len(df) < 30:
            return []
        try:
            from stockstats import wrap

            wrapped = wrap(df.copy())
            series = wrapped[indicator].astype(float)
            dates = wrapped["Date"].dt.strftime("%Y-%m-%d")
            out = []
            for date_str, value in zip(dates, series):
                if value == value:  # not NaN
                    out.append((date_str, round(float(value), 4)))
            return out[-count:]
        except Exception as exc:
            logger.warning("Indicator series failed for %s/%s: %s", symbol, indicator, exc)
            return []

    # ------------------------------------------------------------------
    # 实时行情 (东财 push2 单股)
    # ------------------------------------------------------------------

    def _degraded_quote_from_ohlcv(self, code: str) -> Optional[dict]:
        """实时行情完全不可用时的降级兜底 (§9.1): 用最近 K 线构造近似行情.
        price=最新收盘, prev_close=前一交易日收盘; 涨跌停价按昨收 ±比例计算,
        与真实行情语义一致 (末根确实收于涨/跌停时依然会触发风控拒买/拒卖)."""
        try:
            df = self.get_ohlcv(code, lookback_days=10)
        except Exception:
            return None
        if df is None or len(df) == 0:
            return None
        price = float(df.iloc[-1]["Close"])
        if price <= 0:
            return None
        prev_close = float(df.iloc[-2]["Close"]) if len(df) >= 2 else price
        if prev_close <= 0:
            prev_close = price
        limit_ratio = 0.30 if code.startswith(("4", "8", "92")) else (
            0.20 if code.startswith(("300", "301", "688", "689")) else 0.10
        )
        change_pct = (price / prev_close - 1.0) * 100 if prev_close else 0.0
        return {
            "symbol": code,
            "name": "",
            "price": price,
            "prev_close": prev_close,
            "change_pct": round(change_pct, 2),
            "limit_up": round(prev_close * (1 + limit_ratio), 2),
            "limit_down": round(prev_close * (1 - limit_ratio), 2),
            "limit_ratio": limit_ratio,
            "market_cap": 0.0,
            "float_mcap": 0.0,
            "degraded_from_ohlcv": True,
        }

    def get_realtime_quote(self, symbol: str) -> Optional[dict]:
        """实时行情: {price, prev_close, change_pct, limit_up, limit_down, name}.

        涨跌停价按昨收 ±10%/20%/30% 计算 (主板/创业科创/北交所).
        """
        code = str(symbol).strip().lower()
        for prefix in ("sh", "sz", "bj"):
            code = code.removeprefix(prefix)

        def _fetch() -> Optional[dict]:
            # 主域被网络环境断连时自动回退延迟域 (与板块资金流同一机制)
            from ai_stock.dataflows.a_stock import _push2_get

            market = "1" if code.startswith(("6", "9", "5")) else "0"
            if code.startswith(("4", "8", "92")):
                market = "0"
            params = {
                "secid": f"{market}.{code}",
                "fields": "f43,f44,f45,f46,f57,f58,f60,f107,f116,f117",
                "fltt": "2",
                "invt": "2",
            }
            r = _push2_get("/api/qt/stock/get", params=params, timeout=10)
            d = (r.json() or {}).get("data") or {}
            price = _to_float(d.get("f43"))
            prev_close = _to_float(d.get("f60"))
            if price <= 0 or prev_close <= 0:
                return None
            limit_ratio = 0.30 if code.startswith(("4", "8", "92")) else (
                0.20 if code.startswith(("300", "301", "688", "689")) else 0.10
            )
            change_pct = (price / prev_close - 1.0) * 100 if prev_close else 0.0
            return {
                "symbol": code,
                "name": str(d.get("f58", "")),
                "price": price,
                "prev_close": prev_close,
                "change_pct": round(change_pct, 2),
                "limit_up": round(prev_close * (1 + limit_ratio), 2),
                "limit_down": round(prev_close * (1 - limit_ratio), 2),
                "limit_ratio": limit_ratio,
                "market_cap": _to_float(d.get("f116")),
                "float_mcap": _to_float(d.get("f117")),
            }

        try:
            return self.cache.get_or_fetch(
                f"quote:{code}",
                _fetch,
                ttl=CACHE_TTL["realtime_quote"],
                category="realtime_quote",
                breaker=get_breaker("realtime_quote"),
                allow_fallback_stale=True,
            )
        except Exception:
            # 熔断打开且无过期缓存可兜底时: 降级到最近 K 线收盘 (§9.1),
            # 保证业务步骤给出确定性结论, 而不是任务重试进死信后 flow 挂起.
            degraded = self._degraded_quote_from_ohlcv(code)
            if degraded is not None:
                logger.warning(
                    "realtime quote unavailable for %s; degraded to last OHLCV close", code,
                )
                return degraded
            raise

    def get_batch_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """批量实时行情 (逐个调用, 走同一缓存)."""
        out: dict[str, dict] = {}
        for sym in symbols:
            quote = self.get_realtime_quote(sym)
            if quote:
                out[sym] = quote
        return out

    # ------------------------------------------------------------------
    # 新闻 / 基本面 (文本透传, 由 Agent 的 LLM 消费)
    # ------------------------------------------------------------------

    def get_stock_news(self, symbol: str, hours: int = 48) -> list[dict]:
        """个股相关新闻 (结构化 list, 来自 impact news 采集)."""
        def _fetch() -> list[dict]:
            from ai_stock.tools import fetch_impact_news

            items = fetch_impact_news(
                _market_today(), hours=hours,
            )
            code = str(symbol).lower().removeprefix("sh").removeprefix("sz").removeprefix("bj")
            name_hint = ""
            try:
                quote = self.get_realtime_quote(symbol)
                if quote:
                    name_hint = quote.get("name", "")
            except Exception:
                pass  # 行情不可用时仅按代码匹配, 不能拖垮新闻获取
            matched = []
            for item in items:
                text = f"{item.get('title', '')} {item.get('content', '')}"
                if code in text or (name_hint and name_hint in text):
                    matched.append(item)
            return matched

        return self.cache.get_or_fetch(
            f"stock_news:{symbol}:{hours}h",
            _fetch,
            ttl=CACHE_TTL["news"],
            category="news",
            breaker=get_breaker("stock_news"),
            allow_fallback_stale=True,
        ) or []

    def get_global_news(self, hours: int = 24) -> list[dict]:
        """全球/宏观财经快讯."""
        def _fetch() -> list[dict]:
            from ai_stock.tools import fetch_impact_news

            return fetch_impact_news(_market_today(), hours=hours)

        return self.cache.get_or_fetch(
            f"global_news:{hours}h",
            _fetch,
            ttl=CACHE_TTL["news"],
            category="news",
            breaker=get_breaker("global_news"),
            allow_fallback_stale=True,
        ) or []

    def get_hot_news(self, days: int = 3) -> list[dict]:
        """近 N 日全市场热点新闻/政策快讯 (宏观事件解析用)."""
        hours = int(days) * 24

        def _fetch() -> list[dict]:
            from ai_stock.tools import fetch_impact_news

            return fetch_impact_news(_market_today(), hours=hours)

        return self.cache.get_or_fetch(
            f"hot_news:{days}d",
            _fetch,
            ttl=CACHE_TTL["news"],
            category="news",
            breaker=get_breaker("hot_news"),
            allow_fallback_stale=True,
        ) or []

    def get_fundamentals_text(self, symbol: str) -> str:
        """基本面综合文本 (估值/盈利/财务摘要, 给 LLM 消费)."""
        def _fetch() -> str:
            from ai_stock.tools import fetch_fundamentals

            return fetch_fundamentals(symbol, _market_today())

        return self.cache.get_or_fetch(
            f"fundamentals:{symbol}",
            _fetch,
            ttl=CACHE_TTL["fundamentals"],
            category="fundamentals",
            breaker=get_breaker("fundamentals"),
            allow_fallback_stale=True,
        ) or ""

    def get_lockup_expiry_text(self, symbol: str) -> str:
        """限售解禁日程文本."""
        def _fetch() -> str:
            from ai_stock.tools import fetch_lockup_expiry

            return fetch_lockup_expiry(symbol, _market_today())

        return self.cache.get_or_fetch(
            f"lockup:{symbol}",
            _fetch,
            ttl=CACHE_TTL["forecast"],
            category="forecast",
            breaker=get_breaker("lockup"),
            allow_fallback_stale=True,
        ) or ""

    def get_insider_text(self, symbol: str) -> str:
        """高管/大股东增减持文本."""
        def _fetch() -> str:
            from ai_stock.tools import fetch_insider_transactions

            return fetch_insider_transactions(symbol)

        return self.cache.get_or_fetch(
            f"insider:{symbol}",
            _fetch,
            ttl=CACHE_TTL["forecast"],
            category="forecast",
            breaker=get_breaker("insider"),
            allow_fallback_stale=True,
        ) or ""

    def get_profit_forecast_text(self, symbol: str) -> str:
        """一致预期 EPS / 前瞻 PE / PEG 文本."""
        def _fetch() -> str:
            from ai_stock.tools import fetch_profit_forecast

            # 传市场当前日期: 主机时区偏西时本地日期会落后市场当天,
            # 被数据层 _is_historical 判成复盘, 预测文本会凭空多出未来函数告警.
            return fetch_profit_forecast(symbol, _market_today())

        return self.cache.get_or_fetch(
            f"forecast:{symbol}",
            _fetch,
            ttl=CACHE_TTL["forecast"],
            category="forecast",
            breaker=get_breaker("forecast"),
            allow_fallback_stale=True,
        ) or ""

    def get_concept_blocks(self, symbol: str) -> list[str]:
        """个股所属概念/行业板块名列表."""
        def _fetch() -> list[str]:
            from ai_stock.tools import fetch_concept_blocks

            text = fetch_concept_blocks(symbol) or ""
            # 输出为格式化文本; 提取板块名行 (## xxx / - xxx)
            names = []
            for line in text.splitlines():
                line = line.strip().lstrip("#").lstrip("-").strip()
                if line and 1 <= len(line) <= 20 and not any(
                    ch.isdigit() for ch in line[:2]
                ):
                    names.append(line)
            return names[:40]

        return self.cache.get_or_fetch(
            f"concepts:{symbol}",
            _fetch,
            ttl=CACHE_TTL["fundamentals"],
            category="fundamentals",
            breaker=get_breaker("concepts"),
            allow_fallback_stale=True,
        ) or []

    # ------------------------------------------------------------------
    # 主题雷达 (旁路注入): 个股反查行业 + 全市场微观异动扫描
    # ------------------------------------------------------------------

    def get_stock_industry_code(self, code: str) -> Optional[str]:
        """个股反查所属最细分板块代码 (雷达注入候选池用).

        用个股概念/行业板块名 (get_concept_blocks) 与全板块库 (含 code+name)
        做名称匹配: 概念级板块 (更细分, 如 电源电容/液冷) 优先命中并直接返回;
        否则退回首个行业级命中。无命中/异常返回 None (天然降级, 不阻断主流程)。
        """
        if not code:
            return None
        try:
            names = self.get_concept_blocks(code)
            if not names:
                return None
            boards = self.get_all_board_fund_flow()
            if not boards:
                return None
            best: Optional[str] = None
            for board in boards:
                bcode = board.get("code", "")
                bname = board.get("name", "")
                if not bcode or not bname:
                    continue
                if any(_name_match(bname, n) for n in names):
                    if board.get("board_level") == "concept":
                        return bcode  # 概念级最细分, 直接采用
                    best = best or bcode
            return best
        except Exception as exc:
            logger.warning("get_stock_industry_code failed for %s: %s", code, exc)
            return None

    def scan_micro_anomalies(self) -> dict[str, list[str]]:
        """全市场微观异动扫描: 放量(量比>2) + 温和上涨(3%~8%) + 中小市值(<300亿),
        按概念/行业板块聚类, 返回 {主题名: [异动个股代码]} (捕捉大主题下的隐形冠军)。

        仅扫描主力资金净流入靠前且当日上涨的"温热"板块 (控制在有限板块内, 成分股
        取数走同一缓存)。任何异常/无数据降级为空 dict — 雷达挂掉时选股退化为原版涨幅榜。
        """
        def _fetch() -> dict[str, list[str]]:
            boards = self.get_all_board_fund_flow()
            if not boards:
                return {}
            # 温热板块: 当日上涨(0 < change < 涨幅上沿*2), 主力净流入降序, 概念级更细分
            warm = [
                b for b in boards
                if b.get("code") and b.get("name")
                and 0 < float(b.get("change_pct", 0) or 0) < RADAR_CHANGE_PCT_MAX * 2
            ]
            warm.sort(
                key=lambda b: (
                    b.get("board_level") == "concept",
                    float(b.get("main_net_inflow", 0) or 0),
                ),
                reverse=True,
            )
            clusters: dict[str, list[str]] = {}
            for board in warm[: RADAR_MAX_THEMES * 6]:
                bcode = board.get("code", "")
                bname = board.get("name", "")
                try:
                    stocks = self.get_industry_stocks(bcode, top_n=30)
                except Exception as exc:
                    logger.warning("radar constituents failed for %s: %s", bcode, exc)
                    continue
                hits: list[str] = []
                for s in stocks:
                    scode = str(s.get("code", "") or "")
                    sname = str(s.get("name", "") or "")
                    if not scode or _is_excluded_symbol(scode, sname):
                        continue
                    chg = float(s.get("change_pct", 0) or 0)
                    vr = float(s.get("volume_ratio", 0) or 0)
                    mcap = float(s.get("market_cap", 0) or 0)
                    if (
                        RADAR_CHANGE_PCT_MIN <= chg <= RADAR_CHANGE_PCT_MAX
                        and vr > RADAR_VOLUME_RATIO_MIN
                        and 0 < mcap < RADAR_MARKET_CAP_MAX
                    ):
                        hits.append(scode)
                if len(hits) >= RADAR_MIN_CLUSTER:
                    clusters[bname] = hits
            # 家数降序, 截断到 RADAR_MAX_THEMES
            ranked = sorted(clusters.items(), key=lambda kv: len(kv[1]), reverse=True)
            return {name: codes for name, codes in ranked[:RADAR_MAX_THEMES]}

        try:
            return self.cache.get_or_fetch(
                "micro_anomalies",
                _fetch,
                ttl=CACHE_TTL["fund_flow"],
                category="fund_flow",
                breaker=get_breaker("micro_anomalies"),
                allow_fallback_stale=True,
            ) or {}
        except Exception as exc:
            logger.warning("scan_micro_anomalies degraded to empty: %s", exc)
            return {}


# 北交所代码前缀 (4/8/92) — 与选股硬过滤口径一致, ST/退市/北交所排除
_EXCLUDED_PREFIX = ("4", "8", "92")


def _is_excluded_symbol(code: str, name: str) -> bool:
    """ST / 退市风险 / 北交所排除 (雷达扫描硬过滤)."""
    code = str(code)
    name = str(name)
    return (
        code.startswith(_EXCLUDED_PREFIX)
        or "ST" in name.upper()
        or "退" in name
    )


def _name_match(board_name: str, tag: str) -> bool:
    """板块名与主题/概念名的宽松匹配 (双向包含)."""
    if not board_name or not tag:
        return False
    return tag in board_name or board_name in tag


def _to_float(value) -> float:
    try:
        if value in (None, "", "-"):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def compute_indicators(df) -> dict:
    """在 OHLCV DataFrame 上用 stockstats 计算全部指标, 返回末行 dict."""
    from stockstats import wrap

    wrapped = wrap(df.copy())
    indicators = [
        "boll", "boll_ub", "boll_lb",
        "macd", "macds", "macdh",
        "rsi", "kdjk", "kdjd", "kdj",
        "atr",
        "close_10_ema", "close_50_sma", "close_200_sma",
        "vwma", "mfi",
    ]
    out: dict = {}
    for ind in indicators:
        try:
            series = wrapped[ind].astype(float)
            value = series.iloc[-1]
            out[ind] = None if value != value else round(float(value), 4)
            # 额外携带近 5 日序列 (背离/缩量判断用)
            recent = [
                round(float(v), 4) for v in series.tail(5) if v == v
            ]
            out[f"{ind}_series"] = recent
        except Exception:
            out[ind] = None
            out[f"{ind}_series"] = []
    # 量能序列 (缩量判断)
    try:
        out["volume_series"] = [round(float(v), 0) for v in df["Volume"].tail(5)]
        out["close_series"] = [round(float(v), 2) for v in df["Close"].tail(5)]
        # 高低序列给突破/缺口判断用, 需 ≥11 根 (AddPositionAgent 要求 >=11)
        out["high_series"] = [round(float(v), 2) for v in df["High"].tail(21)]
        out["low_series"] = [round(float(v), 2) for v in df["Low"].tail(21)]
    except Exception:
        pass
    return out


# 模块级单例
_service: Optional[DataService] = None


def get_data_service() -> DataService:
    global _service
    if _service is None:
        _service = DataService()
    return _service
