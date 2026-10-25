"""宏观五变量取数 (M4 的唯一新增数据源).

为什么单独一个模块: 这是策略台唯一需要**外盘/利率**数据的地方, 与 A 股行情
数据源互不隶属. 把它隔离, 挂掉时只影响 regime 判定, 不影响报告主体.

原则: 取不到就返回 None. 绝不填估计值, 也绝不用"宏观不利"反推板块方向.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ticker → 字段名. 汇率/CNH 用 USDCNH=X (离岸人民币).
_TICKERS = {
    "^TNX": "us10y",       # 美债 10Y 收益率 (数值即百分比)
    "DX-Y.NYB": "dxy",
    "USDCNH=X": "usdcnh",
    "CL=F": "oil",
    "^VIX": "vix",
    "^SOX": "sox",
}

# 进程内缓存: 一天内不重复拉, yfinance 对高频请求会限流
_CACHE: dict[str, Optional[float]] = {}
_CACHE_DAY: list[str] = []


def fetch_macro() -> dict[str, Optional[float]]:
    """返回字段 → 数值. 失败字段为 None.

    yfinance 未安装 / 网络不可达 / 单个 ticker 失败, 都只让对应字段变 None,
    不抛异常 —— 缺数据是常态, 不是故障.
    """
    today = _today()
    if _CACHE_DAY and _CACHE_DAY[0] == today and _CACHE:
        return dict(_CACHE)

    values: dict[str, Optional[float]] = {name: None for name in _TICKERS.values()}
    try:
        import yfinance as yf
    except Exception as exc:               # 未安装: 明确记录, 不静默
        logger.warning("yfinance unavailable, macro vars missing: %s", exc)
        return values

    for ticker, name in _TICKERS.items():
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if hist is None or hist.empty:
                continue
            close = float(hist["Close"].dropna().iloc[-1])
            values[name] = round(close, 4)
        except Exception as exc:
            logger.debug("macro ticker %s failed: %s", ticker, exc)

    _CACHE.clear()
    _CACHE.update(values)
    _CACHE_DAY[:] = [today]
    return dict(values)


def _today() -> str:
    from ..quant.calendar_utils import get_trade_date

    try:
        return str(get_trade_date())
    except Exception:
        from datetime import datetime

        return datetime.now().strftime("%Y-%m-%d")
