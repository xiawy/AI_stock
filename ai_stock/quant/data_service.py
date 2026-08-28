"""Structured data service for quant agents (设计文档 §9.1).

把现有 ``ai_stock.dataflows`` 的文本型接口适配成结构化数据, 统一经过
熔断器 + 三级缓存:
- 板块资金流 / 涨停股 / 板块龙头 — 直接复用 pipeline_data 的结构化函数
- K 线 — 解析 get_stock_data 的 CSV 文本 → DataFrame → stockstats 本地算指标
- 实时行情 — 东财 push2 单股接口 (复用 a_stock 的节流 _em_get)
- 外部接口返回 None/异常时返回缓存兜底并打告警日志, 不直接上抛策略层.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from io import StringIO
from typing import Optional

from .cache_manager import get_breaker, get_cache_manager
from .config import CACHE_TTL

logger = logging.getLogger(__name__)


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
            from ai_stock.dataflows.pipeline_data import get_limit_up_stocks

            return get_limit_up_stocks(
                datetime.now().strftime("%Y-%m-%d"), days=days,
            )

        return self.cache.get_or_fetch(
            f"limit_up_{days}d",
            _fetch,
            ttl=CACHE_TTL["fund_flow"],
            category="fund_flow",
            breaker=get_breaker("limit_up"),
            allow_fallback_stale=True,
        ) or []

    def get_board_leaders(self, board_code: str, top_n: int = 6) -> list[dict]:
        """板块龙头股: 领涨+板块最相关+弹性最大 (code/name/change_pct/turnover_rate...)."""
        if not board_code:
            return []

        def _fetch() -> list[dict]:
            from ai_stock.dataflows.pipeline_data import get_industry_leader_stocks

            return get_industry_leader_stocks(board_code, top_n=top_n)

        return self.cache.get_or_fetch(
            f"board_leaders:{board_code}:{top_n}",
            _fetch,
            ttl=CACHE_TTL["realtime_quote"],
            category="fund_flow",
            breaker=get_breaker("board_leaders"),
            allow_fallback_stale=True,
        ) or []

    # ------------------------------------------------------------------
    # 个股 K 线 + 技术指标 (本地 stockstats 计算)
    # ------------------------------------------------------------------

    def get_ohlcv(self, symbol: str, lookback_days: int = 120) -> "object":
        """个股日 K DataFrame (Date/Open/High/Low/Close/Volume), 解析自 get_stock_data."""
        import pandas as pd

        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=int(lookback_days * 1.7) + 15)).strftime(
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
            return df.tail(lookback_days).reset_index(drop=True)

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

            df = pd.DataFrame(cached)
            df["Date"] = pd.to_datetime(df["Date"])
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

    def get_realtime_quote(self, symbol: str) -> Optional[dict]:
        """实时行情: {price, prev_close, change_pct, limit_up, limit_down, name}.

        涨跌停价按昨收 ±10%/20%/30% 计算 (主板/创业科创/北交所).
        """
        code = str(symbol).strip().lower()
        for prefix in ("sh", "sz", "bj"):
            code = code.removeprefix(prefix)

        def _fetch() -> Optional[dict]:
            from ai_stock.dataflows.a_stock import _em_get

            market = "1" if code.startswith(("6", "9", "5")) else "0"
            if code.startswith(("4", "8", "92")):
                market = "0"
            url = "https://push2.eastmoney.com/api/qt/stock/get"
            params = {
                "secid": f"{market}.{code}",
                "fields": "f43,f44,f45,f46,f57,f58,f60,f107,f116,f117",
                "fltt": "2",
                "invt": "2",
            }
            r = _em_get(url, params=params, timeout=10)
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

        return self.cache.get_or_fetch(
            f"quote:{code}",
            _fetch,
            ttl=CACHE_TTL["realtime_quote"],
            category="realtime_quote",
            breaker=get_breaker("realtime_quote"),
            allow_fallback_stale=True,
        )

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
            from ai_stock.dataflows.pipeline_data import get_impact_news

            items = get_impact_news(
                datetime.now().strftime("%Y-%m-%d"), hours=hours,
            )
            code = str(symbol).lower().removeprefix("sh").removeprefix("sz")
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
            from ai_stock.dataflows.pipeline_data import get_impact_news

            return get_impact_news(datetime.now().strftime("%Y-%m-%d"), hours=hours)

        return self.cache.get_or_fetch(
            f"global_news:{hours}h",
            _fetch,
            ttl=CACHE_TTL["news"],
            category="news",
            breaker=get_breaker("global_news"),
            allow_fallback_stale=True,
        ) or []

    def get_fundamentals_text(self, symbol: str) -> str:
        """基本面综合文本 (估值/盈利/财务摘要, 给 LLM 消费)."""
        def _fetch() -> str:
            from ai_stock.dataflows.a_stock import get_fundamentals

            return get_fundamentals(symbol, datetime.now().strftime("%Y-%m-%d"))

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
            from ai_stock.dataflows.a_stock import get_lockup_expiry

            return get_lockup_expiry(symbol, datetime.now().strftime("%Y-%m-%d"))

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
            from ai_stock.dataflows.a_stock import get_insider_transactions

            return get_insider_transactions(symbol)

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
            from ai_stock.dataflows.a_stock import get_profit_forecast

            return get_profit_forecast(symbol)

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
            from ai_stock.dataflows.a_stock import get_concept_blocks

            text = get_concept_blocks(symbol) or ""
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
        out["high_series"] = [round(float(v), 2) for v in df["High"].tail(10)]
        out["low_series"] = [round(float(v), 2) for v in df["Low"].tail(10)]
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
