"""Stock lookup / quote / kline services backed by the A-share dataflows.

Engine imports are lazy: auth-only deployments boot without the data stack.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException, status

from app.core.trading import bootstrap as bootstrap_engine


def _dataflows() -> SimpleNamespace:
    bootstrap_engine()
    from ai_stock.dataflows import a_stock
    from web import stock_display

    return SimpleNamespace(a_stock=a_stock, stock_display=stock_display)


# Process-lifetime stock-name cache: the Tencent quote resolves a name in
# ~0.2s, while the local name map (mootdx-backed) can block for a minute on a
# cold cache — so always try the quote first and keep it locally cached.
_NAME_CACHE: dict[str, str | None] = {}


def _resolve_name(eng: SimpleNamespace, code: str) -> str | None:
    if code in _NAME_CACHE:
        return _NAME_CACHE[code]
    name = None
    try:
        name = eng.a_stock._tencent_quote([code]).get(code, {}).get("name") or None
    except Exception:
        name = None
    if not name:
        try:
            name = eng.stock_display.resolve_stock_name(code)
        except Exception:
            name = None
    _NAME_CACHE[code] = name
    return name


def search(raw: str) -> dict[str, Any]:
    """Resolve user input to (code, name, display label)."""
    eng = _dataflows()
    try:
        code = eng.a_stock.resolve_ticker(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    name = _resolve_name(eng, code)
    return {
        "raw": raw,
        "code": code,
        "name": name,
        "label": f"{code} {name}" if name else code,
    }


def quote(code: str) -> dict[str, Any]:
    """Realtime quote from the Tencent endpoint used by the pipeline."""
    eng = _dataflows()
    code = eng.a_stock._normalize_ticker(code)
    data = eng.a_stock._tencent_quote([code]).get(code, {})
    if not data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"未获取到 {code} 的实时行情"
        )
    return {
        "code": code,
        "name": data.get("name") or _resolve_name(eng, code),
        "price": data.get("price"),
        "change_pct": data.get("change_pct"),
        "pe_ttm": data.get("pe_ttm"),
        "pb": data.get("pb"),
        "mcap_yi": data.get("mcap_yi"),
        "turnover_pct": data.get("turnover_pct"),
        "limit_up": data.get("limit_up"),
        "limit_down": data.get("limit_down"),
    }


def kline(code: str, days: int = 120) -> dict[str, Any]:
    """Daily OHLCV ending today, for the ECharts candlestick chart.

    Uses the same loader as the market analyst (mootdx + sina supplement),
    so chart data and report data come from one source of truth.
    """
    eng = _dataflows()
    code = eng.a_stock._normalize_ticker(code)
    from datetime import datetime
    from zoneinfo import ZoneInfo

    end_date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    try:
        df = eng.a_stock._load_ohlcv_astock(code, end_date)
    except Exception as exc:  # noqa: BLE001 — data source failures are user-facing
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"K线数据获取失败: {exc}",
        ) from exc

    if df is None or df.empty:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"{code} 无K线数据"
        )

    df = df.tail(days)
    items = [
        {
            "date": row["Date"].strftime("%Y-%m-%d")
            if hasattr(row["Date"], "strftime")
            else str(row["Date"]),
            "open": round(float(row["Open"]), 2),
            "high": round(float(row["High"]), 2),
            "low": round(float(row["Low"]), 2),
            "close": round(float(row["Close"]), 2),
            "volume": float(row["Volume"]),
        }
        for _, row in df.iterrows()
    ]
    return {
        "code": code,
        "name": _resolve_name(eng, code),
        "end_date": end_date,
        "items": items,
    }
