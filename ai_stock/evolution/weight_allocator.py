"""Weight allocator: assigns importance weights to agents based on market regime.

In a strong trend, technical (market) and hot-money analysts matter more.
In oscillation, fundamentals and policy analysts carry more weight.
In high volatility or normal conditions, all agents are weighted equally.
"""

from __future__ import annotations

import logging
from typing import Dict

logger = logging.getLogger(__name__)


class WeightAllocator:
    """Assign weights to agents based on market regime."""

    WEIGHT_TABLE: Dict[str, Dict[str, float]] = {
        "strong_trend": {
            "market": 1.5,
            "fundamentals": 0.7,
            "hot_money": 1.3,
            "policy": 0.8,
            "social": 1.0,
            "news": 1.0,
        },
        "oscillation": {
            "market": 0.8,
            "fundamentals": 1.2,
            "hot_money": 0.8,
            "policy": 1.3,
            "social": 1.0,
            "news": 1.0,
        },
        "high_volatility": {
            "market": 1.0,
            "fundamentals": 1.0,
            "hot_money": 1.0,
            "policy": 1.0,
            "social": 1.0,
            "news": 1.0,
        },
        "normal": {
            "market": 1.0,
            "fundamentals": 1.0,
            "hot_money": 1.0,
            "policy": 1.0,
            "social": 1.0,
            "news": 1.0,
        },
    }

    def get_weights(self, regime: str) -> Dict[str, float]:
        """Return the weight dict for a given regime. Falls back to 'normal'."""
        return self.WEIGHT_TABLE.get(regime, self.WEIGHT_TABLE["normal"]).copy()
