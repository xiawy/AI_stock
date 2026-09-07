"""市场微观结构工具 (旁路增强, 全部可降级).

当前提供 ``is_tail_phase`` 精准鱼尾检测器 —— 供 ConfidenceMaintainAgent
(维护官标记 TAIL) 与 LogicCollapseAgent (鱼尾硬拦截) 共用。设计原则:
数据不足或任何异常一律返回 ``False``, 绝不误踢/误拦截 (宁可放过不可错杀)。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def is_tail_phase(data, symbol: str) -> bool:
    """精准鱼尾检测: 高位放量滞涨 (顶背离式派发)。

    基于 ``data.get_ohlcv(symbol, TAIL_LOOKBACK_BARS)`` 的日 K, 四条同时成立
    才判鱼尾:

    1. 放量/高换手: 末根量能 > 前 7 日均量 * ``TAIL_VOLUME_SURGE``;
       实时行情换手率可得时叠加校验 (> ``TAIL_TURNOVER_MIN``), 缺失则
       退化为纯量能代理 (不强判)。
    2. 近 5 日滞涨: ``close[-1]/close[-6] - 1 < TAIL_GAIN_MAX``。
    3. 高位·鱼身已成: 现价较窗口最低收盘累计涨幅
       ``close[-1]/min(close) - 1 >= TAIL_PRIOR_RUNUP_MIN`` (排除从未上涨的平庸票)。
    4. 高位·仍贴顶: 现价距窗口最高
       ``(max(high) - close[-1]) / close[-1] < TAIL_HIGH_DIST_MAX``。

    数据不足 (K 线少于 8 根) 或任何异常一律返回 ``False``。
    """
    from .config import (
        TAIL_GAIN_MAX,
        TAIL_HIGH_DIST_MAX,
        TAIL_LOOKBACK_BARS,
        TAIL_PRIOR_RUNUP_MIN,
        TAIL_TURNOVER_MIN,
        TAIL_VOLUME_SURGE,
    )

    try:
        df = data.get_ohlcv(symbol, TAIL_LOOKBACK_BARS)
    except Exception as exc:  # 数据源异常: 不判鱼尾
        logger.debug("is_tail_phase get_ohlcv failed for %s: %s", symbol, exc)
        return False
    if df is None:
        return False

    try:
        close = [float(x) for x in df["Close"].tolist()]
        high = [float(x) for x in df["High"].tolist()]
        vol = [float(x) for x in df["Volume"].tolist()]
    except Exception as exc:
        logger.debug("is_tail_phase column parse failed for %s: %s", symbol, exc)
        return False

    # 至少 8 根: vol[-8:-1] 取 7 根均量, close[-6] 计算 5 日涨幅
    if len(close) < 8 or len(high) < 8 or len(vol) < 8:
        return False
    if close[-1] <= 0 or close[-6] <= 0:
        return False

    # -- 条件 1: 放量 (末根 vs 前 7 日均量) -----------------------------------
    prev_vol = vol[-8:-1]
    avg_prev_vol = sum(prev_vol) / len(prev_vol) if prev_vol else 0.0
    if avg_prev_vol <= 0 or vol[-1] <= TAIL_VOLUME_SURGE * avg_prev_vol:
        return False
    # 换手率尽力校验: 实时行情可得 turnover_rate 时需 > 门槛, 缺失退化为量能代理
    try:
        quote = data.get_realtime_quote(symbol) or {}
    except Exception:
        quote = {}
    turnover = quote.get("turnover_rate") if isinstance(quote, dict) else None
    if turnover:
        try:
            if float(turnover) <= TAIL_TURNOVER_MIN:
                return False
        except (TypeError, ValueError):
            pass

    # -- 条件 2: 近 5 日滞涨 ---------------------------------------------------
    if close[-1] / close[-6] - 1.0 >= TAIL_GAIN_MAX:
        return False

    # -- 条件 3: 高位·鱼身已成 (现价较窗口最低收盘的累计涨幅) --------------
    positive_close = [c for c in close if c > 0]
    if not positive_close:
        return False
    if close[-1] / min(positive_close) - 1.0 < TAIL_PRIOR_RUNUP_MIN:
        return False

    # -- 条件 4: 高位·仍贴顶 (现价距窗口最高很近, 未明显回落) -------------
    positive_high = [h for h in high if h > 0]
    if not positive_high:
        return False
    window_high = max(positive_high)
    if (window_high - close[-1]) / close[-1] >= TAIL_HIGH_DIST_MAX:
        return False

    return True
