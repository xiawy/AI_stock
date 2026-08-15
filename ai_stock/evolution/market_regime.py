"""Market regime detector: classifies the current market state using CSI 300 data.

The regime classification drives the weight allocator — different market
conditions favour different analyst types.

Regimes:
- ``strong_trend``: 20-day return > 5% (bullish or bearish trend)
- ``oscillation``: moderate volatility, no clear direction
- ``high_volatility``: 20-day volatility > 2x its 60-day average
- ``normal``: default / calm market
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)


class MarketRegimeDetector:
    """Classify market state using CSI 300 (000300.SS) data."""

    REGIMES = ("strong_trend", "oscillation", "high_volatility", "normal")

    def detect(self, trade_date: str) -> Dict[str, Any]:
        """Detect the current market regime.

        Returns a dict with keys: ``regime``, ``confidence``, ``metrics``.
        Falls back to ``"normal"`` if data is unavailable.
        """
        try:
            metrics = self._fetch_metrics(trade_date)
        except Exception:
            logger.warning("Failed to fetch market metrics for %s", trade_date, exc_info=True)
            return {"regime": "normal", "confidence": 0.0, "metrics": {}}

        regime, confidence = self._classify(metrics)
        return {"regime": regime, "confidence": confidence, "metrics": metrics}

    def _fetch_metrics(self, trade_date: str) -> Dict[str, float]:
        """Fetch CSI 300 data and compute volatility / trend / volume metrics."""
        import yfinance as yf
        from datetime import datetime, timedelta

        end = datetime.strptime(trade_date, "%Y-%m-%d")
        start = end - timedelta(days=90)  # need 60+ trading days
        start_str = start.strftime("%Y-%m-%d")
        end_str = end.strftime("%Y-%m-%d")

        bench = yf.Ticker("000300.SS").history(start=start_str, end=end_str)
        if len(bench) < 20:
            return {}

        close = bench["Close"]
        volume = bench["Volume"]

        # 20-day volatility (annualised)
        returns_20d = close.pct_change().dropna().tail(20)
        vol_20d = float(returns_20d.std() * (252 ** 0.5)) if len(returns_20d) > 5 else 0.0

        # 60-day average volatility (for comparison)
        returns_60d = close.pct_change().dropna().tail(60)
        vol_60d_avg = float(returns_60d.std() * (252 ** 0.5)) if len(returns_60d) > 20 else vol_20d

        # 20-day return
        if len(close) >= 20:
            ret_20d = float((close.iloc[-1] - close.iloc[-20]) / close.iloc[-20])
        else:
            ret_20d = 0.0

        # Volume change: recent 5-day avg vs 20-day avg
        vol_recent = float(volume.tail(5).mean()) if len(volume) >= 5 else 0.0
        vol_avg = float(volume.tail(20).mean()) if len(volume) >= 20 else 1.0
        vol_ratio = vol_recent / vol_avg if vol_avg > 0 else 1.0

        return {
            "volatility_20d": round(vol_20d, 4),
            "volatility_60d_avg": round(vol_60d_avg, 4),
            "return_20d": round(ret_20d, 4),
            "volume_ratio": round(vol_ratio, 2),
        }

    def _classify(self, metrics: Dict[str, float]) -> Tuple[str, float]:
        """Rule-based classification from metrics."""
        if not metrics:
            return ("normal", 0.0)

        vol = metrics.get("volatility_20d", 0)
        vol_avg = metrics.get("volatility_60d_avg", vol)
        ret = abs(metrics.get("return_20d", 0))

        # High volatility: current vol > 2x the 60-day average
        if vol_avg > 0 and vol > 2 * vol_avg:
            return ("high_volatility", min(vol / (2 * vol_avg), 1.0))

        # Strong trend: |20d return| > 5%
        if ret > 0.05:
            return ("strong_trend", min(ret / 0.10, 1.0))

        # Oscillation: moderate vol, no clear trend
        if vol > 0.15:
            return ("oscillation", 0.5)

        return ("normal", 0.6)
