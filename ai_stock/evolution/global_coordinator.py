"""Global coordinator: cross-agent coordination layer.

Runs after the main analysis pipeline completes. Combines:
1. Market regime detection (via MarketRegimeDetector)
2. Agent weight allocation (via WeightAllocator)
3. Cross-agent conflict detection (e.g. fundamentals bullish + technical bearish)
4. Global evolution report generation

The report is injected into the Portfolio Manager's prompt (Phase 5) and can
also be written to disk for the CLI / backend to display.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .market_regime import MarketRegimeDetector
from .weight_allocator import WeightAllocator

logger = logging.getLogger(__name__)


class GlobalCoordinator:
    """Cross-agent coordination: conflict detection + global report."""

    def __init__(
        self,
        llm: Any,
        agents: Optional[List[str]] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.regime_detector = MarketRegimeDetector()
        self.weight_allocator = WeightAllocator()
        self.llm = llm
        self.agents = agents or ["market", "social", "news", "fundamentals", "policy", "hot_money"]
        self.config = config or {}

    def run(self, trade_date: str, agent_reports: Dict[str, str]) -> Dict[str, Any]:
        """Run the global coordination step.

        Args:
            trade_date: The analysis date (YYYY-MM-DD).
            agent_reports: Map of agent_name -> report text.

        Returns:
            Dict with regime, weights, conflicts, and report.
        """
        regime = self.regime_detector.detect(trade_date)
        weights = self.weight_allocator.get_weights(regime["regime"])
        conflicts = self._detect_conflicts(agent_reports)
        report = self._generate_report(regime, weights, conflicts)

        return {
            "regime": regime,
            "weights": weights,
            "conflicts": conflicts,
            "report": report,
        }

    def _detect_conflicts(self, agent_reports: Dict[str, str]) -> List[str]:
        """Detect cross-agent conflicts using simple keyword heuristics.

        A more sophisticated approach would use the LLM, but keyword checks
        are fast and cover the most common case (bullish vs bearish signals).
        """
        conflicts = []
        bullish_agents = []
        bearish_agents = []

        for agent, report in agent_reports.items():
            if not report:
                continue
            report_lower = report.lower()
            # Simple keyword-based sentiment detection
            bullish_signals = ["看多", "bullish", "买入", "buy", "上涨", "利好", "增长", "强势"]
            bearish_signals = ["看空", "bearish", "卖出", "sell", "下跌", "利空", "风险", "弱势"]

            bull_count = sum(1 for s in bullish_signals if s in report_lower)
            bear_count = sum(1 for s in bearish_signals if s in report_lower)

            if bull_count > bear_count + 2:
                bullish_agents.append(agent)
            elif bear_count > bull_count + 2:
                bearish_agents.append(agent)

        # Conflicts: agents on opposite sides
        for bull in bullish_agents:
            for bear in bearish_agents:
                conflicts.append(
                    f"{bull} 偏多 vs {bear} 偏空 — 建议 Portfolio Manager 根据市场状态权重裁量"
                )

        return conflicts

    def _generate_report(
        self,
        regime: Dict[str, Any],
        weights: Dict[str, float],
        conflicts: List[str],
    ) -> str:
        """Generate a human-readable global coordination report."""
        lines = [
            "## 全局协同报告",
            "",
            f"**市场状态**: {regime['regime']} (置信度: {regime.get('confidence', 0):.0%})",
            f"**波动率**: {regime.get('metrics', {}).get('volatility_20d', 'N/A')}",
            f"**20日涨跌**: {regime.get('metrics', {}).get('return_20d', 'N/A')}",
            "",
            "### Agent 权重建议",
        ]
        for agent, weight in sorted(weights.items(), key=lambda x: -x[1]):
            bar = "█" * int(weight * 5)
            lines.append(f"- {agent}: {weight:.1f} {bar}")

        if conflicts:
            lines.append("")
            lines.append("### 跨 Agent 冲突")
            for c in conflicts:
                lines.append(f"- ⚠️ {c}")
        else:
            lines.append("")
            lines.append("### 跨 Agent 冲突: 无")

        return "\n".join(lines)
