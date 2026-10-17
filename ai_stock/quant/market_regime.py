"""大盘择时门 (T3): 用沪深300 的均线状态决定「能不能新建仓」。

为什么需要: 全链路原无任何指数趋势过滤。熊市里个股技术信号同样会触发, 系统
会持续抄底 —— 这一条对最大回撤的改善通常比调止损参数更显著。

口径 (刻意保持极简, 避免过拟合):
- ``risk_on``  : 沪深300 收盘 > MA20 且 MA20 相对 3 日前上翘
- ``risk_off`` : 其余情形 (跌破均线或均线走平/下行) → 只减不加
- 数据不可用  : 返回 ``unknown``, 由调用方按「不阻断」处理
  (数据故障不应变成交易开关, 否则数据源抖动会连坐下单)

数据源: 新浪指数日 K (``sh000300``), 与个股 K 线同一套 HTTP 通道;
东财 push2 被封不影响本模块。结果缓存 ``MARKET_REGIME_TTL`` 秒。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "value": None}


def _fetch_index_closes(window: int) -> list[float]:
    """取沪深300 最近 window+10 根日线收盘价 (新浪 HTTP)."""
    from ai_stock.dataflows.a_stock import _sina_kline_fallback

    from .config import MARKET_INDEX_CODE, MARKET_INDEX_PREFIX

    df = _sina_kline_fallback(
        MARKET_INDEX_CODE, prefix=MARKET_INDEX_PREFIX,
    )
    if df is None or len(df) == 0:
        return []
    return [float(v) for v in df["Close"].tolist()][-(window + 10):]


def get_market_regime(force: bool = False) -> dict:
    """返回 ``{regime, close, ma, slope, ok, reason}``.

    ``ok=False`` 表示数据不可用 (``regime='unknown'``), 调用方不应据此拦截交易。
    """
    from .config import (
        MARKET_MA_WINDOW,
        MARKET_REGIME_TTL,
        MARKET_TIMING_ENABLED,
    )

    if not MARKET_TIMING_ENABLED:
        return {
            "regime": "risk_on", "ok": True, "disabled": True,
            "reason": "大盘择时门已关闭 (MARKET_TIMING_ENABLED=False)",
        }

    now = time.monotonic()
    with _lock:
        if (
            not force
            and _cache["value"] is not None
            and now - _cache["ts"] < MARKET_REGIME_TTL
        ):
            return dict(_cache["value"])

    window = int(MARKET_MA_WINDOW)
    try:
        closes = _fetch_index_closes(window)
    except Exception as exc:
        logger.warning("market regime index fetch failed: %s", exc)
        closes = []

    if len(closes) < window + 3:
        value = {
            "regime": "unknown", "ok": False,
            "reason": f"沪深300 日线不足 ({len(closes)} < {window + 3})",
            "close": closes[-1] if closes else None,
            "ma": None, "slope": None,
        }
        _store(value)
        return dict(value)

    ma_now = sum(closes[-window:]) / window
    ma_prev = sum(closes[-window - 3:-3]) / window
    close = closes[-1]
    slope = (ma_now / ma_prev - 1.0) if ma_prev > 0 else 0.0
    above = close > ma_now
    rising = slope > 0

    if above and rising:
        regime, reason = "risk_on", "沪深300 站上 MA20 且均线上翘"
    elif not above:
        regime, reason = "risk_off", f"沪深300 {close:.2f} 跌破 MA20 {ma_now:.2f}"
    else:
        regime, reason = "risk_off", "MA20 走平/下行, 趋势未确认"

    value = {
        "regime": regime, "ok": True, "reason": reason,
        "close": round(close, 2), "ma": round(ma_now, 2),
        "slope": round(slope, 5),
    }
    _store(value)
    return dict(value)


def _store(value: dict) -> None:
    with _lock:
        _cache["ts"] = time.monotonic()
        _cache["value"] = value


def reset_cache() -> None:
    """测试辅助: 清空缓存."""
    with _lock:
        _cache["ts"] = 0.0
        _cache["value"] = None
