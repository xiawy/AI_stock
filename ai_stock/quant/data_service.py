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
import threading
import time
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


def _market_now() -> datetime:
    """A 股市场当前时间 (带时区)."""
    return datetime.now(_MARKET_TZ)


def _session_progress(dt: Optional[datetime] = None) -> float:
    """当日交易时段已走过的比例 (0~1), 用于把盘中成交量折算成全天量级。

    A 股日线接口在盘中不返回当天的未完成 K 线（mootdx 日线只给已收盘的 bar），
    于是「10 分钟扫一次」这个调度全天拿到的都是昨天收盘价 —— 技术信号天然滞后
    一天（2026-10-20 实测：688002 全天 24 次扫描 close 恒为 157.79，实际收
    160.95）。这里按实时价补一根 provisional bar，成交量则需要按已过时段折算，
    否则上午 10 点的量永远小于全天均量，「放量突破」通道整个上午都打不开。
    """
    from .config import AFTERNOON_SESSION, MORNING_SESSION

    dt = dt or _market_now()
    minutes = dt.hour * 60 + dt.minute
    m_start = MORNING_SESSION[0] * 60 + MORNING_SESSION[1]
    m_end = MORNING_SESSION[2] * 60 + MORNING_SESSION[3]
    a_start = AFTERNOON_SESSION[0] * 60 + AFTERNOON_SESSION[1]
    a_end = AFTERNOON_SESSION[2] * 60 + AFTERNOON_SESSION[3]
    total = (m_end - m_start) + (a_end - a_start)
    if total <= 0:
        return 1.0
    if minutes <= m_start:
        return 0.0
    if minutes <= m_end:
        elapsed = minutes - m_start
    elif minutes < a_start:
        elapsed = m_end - m_start            # 午休: 按上午收盘计
    elif minutes <= a_end:
        elapsed = (m_end - m_start) + (minutes - a_start)
    else:
        return 1.0
    return max(0.0, min(elapsed / total, 1.0))


def _quote_volume_estimate(quote: dict, price: float, progress: float) -> float:
    """用实时换手率 × 流通股本反推成交量, 并按已过时段折算到全天量级。

    换手率 = 成交量 / 流通股本, 因此 成交量 = 换手率 × 流通股本。数据源只给
    「当日累计换手」, 盘中除以时段进度即得全天等效量 (收盘后 progress=1)。
    任一字段缺失返回 0.0 —— 调用方退化, 不猜。
    """
    turnover = _to_float((quote or {}).get("turnover_pct"))
    float_mcap = _to_float((quote or {}).get("float_mcap"))
    if turnover <= 0 or float_mcap <= 0 or price <= 0:
        return 0.0
    float_shares = float_mcap / price
    volume = turnover / 100.0 * float_shares
    if progress < 0.1:          # 刚开盘一两分钟, 折算系数太小会放大成噪音
        progress = 0.1
    return volume / progress


def _merge_provisional_bar(df, quote: dict, now: Optional[datetime] = None):
    """把实时行情拼成当天的 provisional bar, 追加到日 K 末尾（或在末尾就地更新）。

    设计取舍：

    - **不改缓存里的日 K。** 拼接只在读取时发生, 原始数据保持"供应商口径",
      避免盘中价被写进缓存后被当成收盘价参与回测或落库。
    - **一致性校验。** 只有当 ``quote.prev_close`` 与日 K 末根收盘一致（或日 K
      末根已经是今天）时才拼接; 否则说明两个数据源不同步, 宁可不拼 —— 拼错
      一根 bar 比少一根更危险。
    - **成交量按已过时段折算**（见 ``_session_progress``）, 换手率拿不到时退化
      为近 20 日均量（等同于"量比 = 1"）, 既不会误报放量, 也不会把缩量形态
      全判成 False 而导致信号全灭。
    """
    import pandas as pd

    if df is None or len(df) == 0 or not quote:
        return df
    if quote.get("degraded_from_ohlcv"):
        # 这段行情本身就是用日 K 兜底造出来的，再拼回日 K 只会造出一根假 bar
        return df
    price = _to_float(quote.get("price"))
    if price <= 0:
        return df
    now = now or _market_now()
    today = now.strftime("%Y-%m-%d")
    try:
        last_date = pd.to_datetime(df["Date"].iloc[-1]).strftime("%Y-%m-%d")
    except Exception:
        return df
    if last_date > today:
        return df                       # 未来数据: 不动
    prev_close = _to_float(quote.get("prev_close"))
    same_session = last_date == today
    if not same_session and prev_close > 0:
        last_close = _to_float(df["Close"].iloc[-1])
        if abs(last_close - prev_close) / prev_close > 0.005:
            # 日 K 末根与实时行情的"昨收"对不上: 两个源不同步, 不拼
            logger.debug(
                "provisional bar skipped: last close %.3f != prev_close %.3f",
                last_close, prev_close,
            )
            return df

    progress = _session_progress(now)
    open_p = _to_float(quote.get("open")) or price
    high_p = max(_to_float(quote.get("high")) or price, price)
    low_p = _to_float(quote.get("low")) or price
    low_p = min(low_p, price) if low_p > 0 else price
    volume = _quote_volume_estimate(quote, price, progress)
    if volume <= 0:
        tail = [float(v) for v in df["Volume"].tail(20).tolist()]
        volume = sum(tail) / len(tail) if tail else 0.0
    row = {
        "Date": pd.Timestamp(today),
        "Open": round(open_p, 2),
        "High": round(high_p, 2),
        "Low": round(low_p, 2),
        "Close": round(price, 2),
        "Volume": float(volume),
    }
    if same_session:
        # 末根就是今天: 就地覆盖成最新的盘中值（保留已有的更低 low / 更高 high）
        for col, value in row.items():
            if col == "Date":
                continue
            if col in ("High", "Low") and col in df.columns:
                existing = _to_float(df[col].iloc[-1])
                if existing > 0:
                    value = max(existing, value) if col == "High" else min(existing, value)
            df.loc[df.index[-1], col] = value
        return df
    return pd.concat([df, pd.DataFrame([row])], ignore_index=True)


# ---------------------------------------------------------------------------
# 实时行情: 多源取数 (P0-1 延伸)
# ---------------------------------------------------------------------------
# 东财 push2 的个股接口 (/api/qt/stock/get) 与板块列表 (clist) 同属一个集群,
# 实测会一起对服务器 IP 断连。若只修板块列表, 实时价拿不到就只能退过期缓存 ——
# 涨跌停判断、单笔金额校验、ATR 定量全部失真, 比"拿不到板块"更危险。
# 因此这里做「东财 → 腾讯」双源, 且把涨跌停价交给数据源给的口径。


def _normalize_symbol(symbol: str) -> str:
    code = str(symbol or "").strip().lower()
    for prefix in ("sh", "sz", "bj"):
        code = code.removeprefix(prefix)
    return code


def _limit_ratio_for(code: str) -> float:
    if code.startswith(("4", "8", "92")):
        return 0.30          # 北交所
    if code.startswith(("300", "301", "688", "689")):
        return 0.20          # 创业板 / 科创板
    return 0.10


def _quote_from_eastmoney(code: str) -> Optional[dict]:
    """东财 push2 单股行情 (主源). 失败直接抛, 由上层回退。"""
    from ai_stock.dataflows.a_stock import _push2_get

    market = "1" if code.startswith(("6", "9", "5")) else "0"
    if code.startswith(("4", "8", "92")):
        market = "0"
    params = {
        "secid": f"{market}.{code}",
        "fields": "f43,f44,f45,f46,f57,f58,f60,f107,f116,f117,f168",
        "fltt": "2",
        "invt": "2",
    }
    r = _push2_get("/api/qt/stock/get", params=params, timeout=10)
    d = (r.json() or {}).get("data") or {}
    price = _to_float(d.get("f43"))
    prev_close = _to_float(d.get("f60"))
    if price <= 0 or prev_close <= 0:
        return None
    limit_ratio = _limit_ratio_for(code)
    change_pct = (price / prev_close - 1.0) * 100 if prev_close else 0.0
    return {
        "symbol": code,
        "name": str(d.get("f58", "")),
        "price": price,
        "prev_close": prev_close,
        "open": _to_float(d.get("f46")),
        "high": _to_float(d.get("f44")),
        "low": _to_float(d.get("f45")),
        "change_pct": round(change_pct, 2),
        "turnover_pct": _to_float(d.get("f168")),
        "limit_up": round(prev_close * (1 + limit_ratio), 2),
        "limit_down": round(prev_close * (1 - limit_ratio), 2),
        "limit_ratio": limit_ratio,
        "market_cap": _to_float(d.get("f116")),
        "float_mcap": _to_float(d.get("f117")),
        "source": "eastmoney_push2",
    }


def _quote_from_tencent(code: str) -> Optional[dict]:
    """腾讯行情 (备源, 不封 IP). 涨跌停价取交易所口径 (含 ST 5% 等)。"""
    from ai_stock.dataflows.a_stock import _tencent_quote

    row = (_tencent_quote([code]) or {}).get(code) or {}
    price = _to_float(row.get("price"))
    prev_close = _to_float(row.get("last_close"))
    if price <= 0 or prev_close <= 0:
        return None
    limit_ratio = _limit_ratio_for(code)
    limit_up = _to_float(row.get("limit_up")) or round(prev_close * (1 + limit_ratio), 2)
    limit_down = _to_float(row.get("limit_down")) or round(prev_close * (1 - limit_ratio), 2)
    change_pct = _to_float(row.get("change_pct"))
    if not change_pct:
        change_pct = (price / prev_close - 1.0) * 100
    return {
        "symbol": code,
        "name": str(row.get("name", "")),
        "price": price,
        "prev_close": prev_close,
        "open": _to_float(row.get("open")),
        "high": _to_float(row.get("high")),
        "low": _to_float(row.get("low")),
        "change_pct": round(change_pct, 2),
        "turnover_pct": _to_float(row.get("turnover_pct")),
        "limit_up": limit_up,
        "limit_down": limit_down,
        "limit_ratio": limit_ratio,
        "market_cap": 0.0,      # 腾讯给的是亿元口径, 与东财元口径不同, 不混用
        # 流通市值按元口径换算, 供「换手率反推成交量」使用 (provisional bar)
        "float_mcap": _to_float(row.get("float_mcap_yi")) * 1e8,
        "source": "tencent",
    }


def _fetch_realtime_quote(code: str) -> Optional[dict]:
    """多源取实时行情: 东财 → 腾讯。两源都失败时抛最后一个异常，交给熔断层。

    空结果按失败处理（接口"成功但没数据"是最危险的静默故障形态）。

    东财失败会进入与板块列表同一套健康矩阵退避（默认连续 3 次 → 5 分钟）：
    ``_push2_get`` 每次要撞主域 + 延迟域、单次超时 10s，若不退避，一轮买入
    扫描里每只票都白等 20s，扫描直接拖过交易时段。
    """
    from ai_stock.dataflows import data_health

    last_error: Optional[Exception] = None
    providers = []
    if data_health.in_backoff("eastmoney_push2", "quote") <= 0:
        providers.append(("eastmoney_push2", _quote_from_eastmoney))
    providers.append(("tencent", _quote_from_tencent))

    for name, fn in providers:
        started = time.monotonic()
        try:
            quote = fn(code)
        except Exception as exc:
            last_error = exc
            logger.warning("realtime quote %s via %s failed: %s", code, name, exc)
            if name == "eastmoney_push2":
                _record_quote_fail(code, exc)
            continue
        if quote:
            if name == "eastmoney_push2":
                data_health.record_ok(
                    name, "quote", (time.monotonic() - started) * 1000.0,
                )
            elif last_error is not None:
                logger.info("realtime quote %s served by tencent (primary unavailable)", code)
            return quote
        logger.warning("realtime quote %s via %s returned empty", code, name)

    if last_error is not None:
        raise last_error
    return None


def _record_quote_fail(code: str, exc: Exception) -> None:
    """东财实时行情失败计入健康矩阵（与板块列表共用退避档位）。"""
    from ai_stock.dataflows import data_health

    try:
        from .config import DATA_SOURCE_BACKOFF_STEPS, DATA_SOURCE_FAIL_THRESHOLD

        data_health.record_fail(
            "eastmoney_push2", "quote", str(exc),
            backoff_steps=tuple(DATA_SOURCE_BACKOFF_STEPS),
            threshold=DATA_SOURCE_FAIL_THRESHOLD,
        )
    except Exception:  # pragma: no cover - 统计失败不影响主流程
        pass


class DataService:
    """量化 Agent 共用的结构化数据门面 (单例见 get_data_service)."""

    def __init__(self):
        self.cache = get_cache_manager()
        # 板块列表的进程内最短采样间隔 (秒): 缓存 TTL 之外的二次护栏。
        # 反爬封禁多由「高频轮询」触发, 缓存击穿时也必须有下限。
        self._board_last_fetch = 0.0
        self._board_last_payload: list[dict] = []
        self._board_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 板块 / 行业
    # ------------------------------------------------------------------

    def get_all_board_fund_flow(self) -> list[dict]:
        """行业+概念板块行情与主力净流入 (结构化: code/name/change_pct/main_net_inflow...).

        多源容错已在 ``pipeline_data`` 内完成 (东财 push2 → 新浪)。本层额外提供:
        - 进程内最短采样间隔 (BOARD_FLOW_MIN_INTERVAL), 防止缓存击穿时的硬撞;
        - 全源失败时用上次成功的内存快照兜底, 避免上游选股链路被 0 条数据打断。
        """
        def _fetch() -> list[dict]:
            from ai_stock.dataflows.pipeline_data import get_all_board_fund_flow

            return get_all_board_fund_flow()

        from .config import BOARD_FLOW_MIN_INTERVAL

        with self._board_lock:
            elapsed = time.monotonic() - self._board_last_fetch
            if (
                self._board_last_payload
                and elapsed < BOARD_FLOW_MIN_INTERVAL
                and not self.cache.get("board_fund_flow")
            ):
                # 间隔内且缓存已被清空: 直接复用上次成功快照, 不回源
                return list(self._board_last_payload)
        try:
            payload = self.cache.get_or_fetch(
                "board_fund_flow",
                _fetch,
                ttl=CACHE_TTL["fund_flow"],
                category="fund_flow",
                breaker=get_breaker("board_fund_flow"),
                allow_fallback_stale=True,
            ) or []
        except Exception as exc:
            logger.warning("board fund flow unavailable, use last snapshot: %s", exc)
            payload = list(self._board_last_payload)
        if payload:
            with self._board_lock:
                self._board_last_fetch = time.monotonic()
                self._board_last_payload = list(payload)
        return payload

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

    def get_ohlcv(
        self, symbol: str, lookback_days: int = 120, live: bool = False,
    ) -> "object":
        """个股日 K DataFrame (Date/Open/High/Low/Close/Volume), 解析自 get_stock_data.

        ``live=True`` 时把实时行情拼成当天的 provisional bar（见
        ``_merge_provisional_bar``）—— 盘中决策必须用它，否则拿到的永远是
        昨天收盘价（日线接口盘中不返回未完成的 bar）。
        """
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
            if live:
                try:
                    quote = self.get_realtime_quote(symbol)
                except Exception as exc:
                    logger.debug("live quote unavailable for %s: %s", symbol, exc)
                    quote = None
                if quote:
                    df = _merge_provisional_bar(df, quote)
            return df
        except Exception as exc:
            logger.warning("ohlcv cache decode failed for %s: %s", symbol, exc)
            return None

    def get_stock_indicators(
        self,
        symbol: str,
        lookback_days: int = 120,
        live: bool = True,
    ) -> Optional[dict]:
        """基于日 K 本地计算技术指标 (stockstats), 返回最近一根 K 线的指标 dict.

        指标: boll(中轨)/boll_ub/boll_lb, macd/macds/macdh, rsi, kdjk/kdjd/kdj,
        atr, close_10_ema, close_50_sma, close_200_sma, volume 比率.

        ``live=True``（默认）会把实时价拼成当天的 provisional bar 再算 —— 盘中
        信号必须反映现价。收盘后拿到的最后一根就是今天，行为与原来一致。
        """
        df = self.get_ohlcv(symbol, lookback_days, live=live)
        if df is None or len(df) < 30:
            return None
        try:
            indicators = compute_indicators(df)
            # 观测字段: 排查"信号用的是哪天的数据"时无需再翻缓存表
            indicators["bar_date"] = str(df["Date"].iloc[-1])[:10]
            indicators["provisional"] = bool(
                indicators["bar_date"] == _market_today()
            )
            return indicators
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

        多源取数 (P0-1 延伸): 东财 push2 个股接口 → 腾讯 qt.gtimg.cn。
        实测东财 push2 整段（clist + stock/get）会对服务器 IP 断连，只修板块
        列表不够 —— 实时价拿不到就只能退过期缓存，涨跌停判断与金额校验全失真。

        涨跌停价优先用数据源给的交易所口径（腾讯 f47/f48 已含 ST 5% 等特殊
        比例），缺失时按昨收 ±10%/20%/30% 兜底 (主板/创业科创/北交所)。
        """
        code = _normalize_symbol(symbol)
        try:
            return self.cache.get_or_fetch(
                f"quote:{code}",
                lambda: _fetch_realtime_quote(code),
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

    from .config import TREND_MA_WINDOW

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
    # 指标序列长度: 背离/缩量判断需 ≥ 20 根, 原 5 根只够跟最近 4 天比 (P1-2)
    series_len = max(30, TREND_MA_WINDOW + 5)
    for ind in indicators:
        try:
            series = wrapped[ind].astype(float)
            value = series.iloc[-1]
            out[ind] = None if value != value else round(float(value), 4)
            # 额外携带近 N 日序列 (背离/缩量判断用)
            recent = [
                round(float(v), 4) for v in series.tail(series_len) if v == v
            ]
            out[f"{ind}_series"] = recent
        except Exception:
            out[ind] = None
            out[f"{ind}_series"] = []
    try:
        close_list = [float(v) for v in df["Close"].tolist()]
        volume_list = [float(v) for v in df["Volume"].tolist()]
        out["close_series"] = [round(v, 2) for v in close_list[-series_len:]]
        out["volume_series"] = [round(v, 0) for v in volume_list[-series_len:]]
        # 高低序列给突破/缺口判断用, 需 ≥11 根 (AddPositionAgent 要求 >=11);
        # 与 close/volume 保持同长度, 避免「用 5 根收盘价判断 11 根突破」的隐性 Bug
        out["high_series"] = [
            round(float(v), 2) for v in df["High"].tail(series_len)
        ]
        out["low_series"] = [
            round(float(v), 2) for v in df["Low"].tail(series_len)
        ]

        # -- 趋势动量通道所需的均线/量能派生量 (P1-2) -----------------------
        window = int(TREND_MA_WINDOW)
        if len(close_list) >= window + 3:
            ma_now = sum(close_list[-window:]) / window
            ma_prev = sum(close_list[-window - 3:-3]) / window
            out["ma20"] = round(ma_now, 4)
            out["ma20_prev"] = round(ma_prev, 4)
            out["ma20_slope"] = (
                round(ma_now / ma_prev - 1.0, 5) if ma_prev > 0 else 0.0
            )
        if len(volume_list) >= window + 1:
            base = volume_list[-window - 1:-1]   # 不含末根, 避免放量自我稀释
            avg = sum(base) / len(base) if base else 0.0
            out["vol_ma20_prev"] = round(avg, 2)
            out["volume_ratio_20"] = (
                round(volume_list[-1] / avg, 3) if avg > 0 else 0.0
            )
        if out.get("atr") and close_list and close_list[-1] > 0:
            out["atr_pct"] = round(float(out["atr"]) / close_list[-1], 4)
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
