"""大盘择时门 (T3): 用沪深300 的均线状态决定「能不能新建仓」。

为什么需要: 全链路原无任何指数趋势过滤。熊市里个股技术信号同样会触发, 系统
会持续抄底 —— 这一条对最大回撤的改善通常比调止损参数更显著。

口径 (刻意保持极简, 避免过拟合):
- ``risk_on``  : 沪深300 收盘 > MA20 ×（1+缓冲）且 MA20 相对 3 日前上翘,
                 并**连续 ``MARKET_CONFIRM_DAYS`` 个交易日**成立
- ``risk_off`` : 其余情形 (跌破均线/均线走平下行/确认天数不足) → 只减不加
- 数据不可用  : 返回 ``unknown``, 由调用方按「不阻断」处理
  (数据故障不应变成交易开关, 否则数据源抖动会连坐下单)

收紧要快、放松要慢 (D8): 单指数 + 单均线在均线附近会反复开关闸门, 缓冲把
「贴着 MA20 震荡」直接排除在 risk_on 之外; risk_off 不设确认, 走弱当下即生效。

数据源: 新浪指数日 K (``sh000300``), 与个股 K 线同一套 HTTP 通道;
东财 push2 被封不影响本模块。结果缓存 ``MARKET_REGIME_TTL`` 秒。
确认天数与 unknown 计数持久化在 ``quant_system_config['market_regime_state']``,
进程重启不会把「连续 2 日」的进度清零。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "value": None}

# 择时确认状态 (JSON): {risk_on_streak, streak_day, unknown_since, unknown_days, unknown_day}
CONFIRM_STATE_KEY = "market_regime_state"


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
    """返回 ``{regime, close, ma, slope, ok, reason, ...}``.

    ``ok=False`` 表示数据不可用 (``regime='unknown'``), 调用方不应据此拦截交易。
    """
    from .config import (
        MARKET_CANDIDATE_BUFFER,
        MARKET_CONFIRM_DAYS,
        MARKET_MA_WINDOW,
        MARKET_REGIME_TTL,
        MARKET_TIMING_ENABLED,
        MARKET_UNKNOWN_WARN_HOURS,
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
        value = _unknown_value(
            closes[-1] if closes else None, window, len(closes), MARKET_UNKNOWN_WARN_HOURS,
        )
        _store(value)
        return dict(value)

    confirm_days = max(1, _config_int("market_confirm_days", MARKET_CONFIRM_DAYS))
    buffer = max(0.0, _config_float("market_buffer", MARKET_CANDIDATE_BUFFER))

    ma_now = sum(closes[-window:]) / window
    ma_prev = sum(closes[-window - 3:-3]) / window
    close = closes[-1]
    slope = (ma_now / ma_prev - 1.0) if ma_prev > 0 else 0.0
    above = close > ma_now * (1 + buffer)
    rising = slope > 0

    state = _load_state()
    today = _today()
    if above and rising:
        streak = int(state.get("risk_on_streak") or 0)
        # 同一交易日只计一次: 盘中每 10 分钟调用一次不能把「2 天」算成「2 次」
        if state.get("streak_day") != today:
            streak += 1
        state.update({"risk_on_streak": streak, "streak_day": today})
        if streak >= confirm_days:
            regime = "risk_on"
            reason = (
                f"沪深300 {close:.2f} 站上 MA20 {ma_now:.2f} (缓冲 {buffer:.0%}) 且上翘, "
                f"连续确认 {streak}/{confirm_days} 日"
            )
        else:
            regime = "risk_off"
            reason = (
                f"沪深300 站上 MA20 但仅确认 {streak}/{confirm_days} 日, 趋势未确认"
            )
    else:
        state.update({"risk_on_streak": 0, "streak_day": today})
        if not above:
            regime, reason = "risk_off", f"沪深300 {close:.2f} 跌破 MA20 {ma_now:.2f}"
        else:
            regime, reason = "risk_off", "MA20 走平/下行, 趋势未确认"

    # 数据恢复: 清掉 unknown 计时, 否则「仓位减半」会一直挂到重启
    state.pop("unknown_since", None)
    state.pop("unknown_days", None)
    state.pop("unknown_day", None)
    _save_state(state)

    value = {
        "regime": regime, "ok": True, "reason": reason,
        "close": round(close, 2), "ma": round(ma_now, 2),
        "slope": round(slope, 5),
        "risk_on_streak": int(state.get("risk_on_streak") or 0),
        "confirm_days": confirm_days,
        "buffer": buffer,
        "unknown_days": int(state.get("unknown_days") or 0),
    }
    _store(value)
    return dict(value)


def unknown_penalty_coef() -> float:
    """大盘数据长期不可用时的仓位系数 (只收紧、不阻断).

    连续 ``MARKET_UNKNOWN_HALF_DAYS`` 个交易日拿不到指数数据, 等于在「不知道
    大盘方向」的情况下满仓开新仓 —— 数据故障不该关掉交易, 但也不该当作一切正常。
    读持久化状态, 不触发任何网络请求。
    """
    from .config import (
        MARKET_UNKNOWN_HALF_DAYS,
        MARKET_UNKNOWN_HALF_FACTOR,
    )

    state = _load_state()
    if int(state.get("unknown_days") or 0) >= int(MARKET_UNKNOWN_HALF_DAYS):
        return float(MARKET_UNKNOWN_HALF_FACTOR)
    return 1.0


def _unknown_value(close: Optional[float], window: int, got: int, warn_hours: int) -> dict:
    """数据不可用: 维护 unknown 计数并按持续时长告警 (不阻断交易)."""
    state = _load_state()
    now = _now()
    since = _parse_iso(state.get("unknown_since"))
    if since is None:
        state["unknown_since"] = now.isoformat(timespec="seconds")
        since = now
    today = _today()
    days = int(state.get("unknown_days") or 0)
    if state.get("unknown_day") != today and _is_trading_day(now):
        days += 1
        state["unknown_day"] = today
    state["unknown_days"] = days
    _save_state(state)

    hours = max(0.0, (now - since).total_seconds() / 3600)
    if hours >= warn_hours:
        from .ops_monitor import ERROR, get_alerts

        get_alerts().emit(
            ERROR, "market_regime_unknown",
            f"大盘择时数据已持续 {hours:.0f} 小时不可用 (连续 {days} 个交易日), "
            f"择时门本次不拦截交易",
        )
    return {
        "regime": "unknown", "ok": False,
        "reason": f"沪深300 日线不足 ({got} < {window + 3}), 已持续 {days} 个交易日",
        "close": close, "ma": None, "slope": None,
        "risk_on_streak": 0, "unknown_days": days,
    }


def _store(value: dict) -> None:
    with _lock:
        _cache["ts"] = time.monotonic()
        _cache["value"] = value


# ---------------------------------------------------------------------------
# 确认状态持久化
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    from . import db_ops

    raw = db_ops.get_config_value(CONFIRM_STATE_KEY, "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("market_regime_state 解析失败, 忽略并重新计数")
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    from . import db_ops

    try:
        db_ops.set_config_value(
            CONFIRM_STATE_KEY,
            json.dumps(state, ensure_ascii=False, sort_keys=True),
            "大盘择时确认状态 (连续天数 / unknown 计数)",
        )
    except Exception as exc:  # 状态丢失只影响确认节奏, 不影响交易
        logger.warning("market_regime_state 写入失败: %s", exc)


def _reset_state_for_tests() -> None:
    """测试辅助: 清空持久化确认状态."""
    from . import db_ops

    db_ops.set_config_value(CONFIRM_STATE_KEY, "{}")


def _config_int(key: str, default: int) -> int:
    from . import db_ops

    try:
        return int(float(db_ops.get_config_value(key, str(default))))
    except (TypeError, ValueError):
        return int(default)


def _config_float(key: str, default: float) -> float:
    from . import db_ops

    try:
        return float(db_ops.get_config_value(key, str(default)))
    except (TypeError, ValueError):
        return float(default)


def _now() -> datetime:
    from .calendar_utils import market_now

    return market_now()


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


def _is_trading_day(dt: datetime) -> bool:
    from .calendar_utils import is_trading_day

    return is_trading_day(dt)


def _parse_iso(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=_now().tzinfo)


def reset_cache() -> None:
    """测试辅助: 清空缓存."""
    with _lock:
        _cache["ts"] = 0.0
        _cache["value"] = None
