"""EvolutionWrapper: transparently wraps any agent node to add evolution capabilities.

Before the original node runs, the wrapper:
  1. Builds an evolution context from the agent's memory system (custom strategies
     + similar past episodes)
  2. Injects it into ``state["evolution_context"]``

After the original node completes, the wrapper:
  3. Records an episode snapshot to the agent's experience store

When ``evolution_enabled`` is False in config, the wrapper degrades to a pure
pass-through — no context injection, no episode recording.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .memory_system import AgentMemorySystem

logger = logging.getLogger(__name__)

# Report keys used across analysts — shared constant to avoid duplication.
_REPORT_KEYS = (
    "market_report", "sentiment_report", "news_report",
    "fundamentals_report", "policy_report", "hot_money_report",
    "investment_plan", "trader_investment_plan",
    "final_trade_decision",
)


class EvolutionWrapper:
    """Wraps any agent node to add evolution capabilities.

    Usage::

        node = create_market_analyst(llm)
        wrapped = EvolutionWrapper("market", node, memory_system)
        # wrapped(state) → loads strategy, retrieves episodes,
        #                  injects into state["evolution_context"],
        #                  runs original node, records episode
    """

    def __init__(
        self,
        agent_name: str,
        node_fn: Callable,
        memory: AgentMemorySystem,
        enabled: bool = True,
    ) -> None:
        self.agent_name = agent_name
        self.node_fn = node_fn
        self.memory = memory
        self.enabled = enabled

    def __call__(self, state: dict, *args: Any, **kwargs: Any) -> dict:
        if not self.enabled:
            return self.node_fn(state, *args, **kwargs)

        # Pre: build evolution context
        ticker = state.get("company_of_interest", "")
        trade_date = state.get("trade_date", "")
        try:
            ctx = self.memory.build_evolution_context(ticker, trade_date)
        except Exception:
            logger.warning(
                "EvolutionWrapper: failed to build context for agent '%s'",
                self.agent_name,
                exc_info=True,
            )
            ctx = ""

        # Inject into state (TypedDict allows extra keys at runtime)
        if ctx:
            state = {**state, "evolution_context": ctx}

        # Run original agent
        result = self.node_fn(state, *args, **kwargs)

        # Post: record episode (outcome is "pending" until reflection resolves it)
        try:
            output_summary = self._extract_summary(result)
            # Build a descriptive input_summary for better Chroma retrieval
            # diversity (instead of always "Analysis of {ticker}").
            input_parts = [f"{self.agent_name} analysis of {ticker} on {trade_date}"]
            # Include which upstream reports were available as context
            available = [k for k in _REPORT_KEYS[:6] if state.get(k)]
            if available:
                input_parts.append(f"upstream: {','.join(available)}")
            input_summary = "; ".join(input_parts)

            self.memory.episodic.record({
                "ticker": ticker,
                "date": trade_date,
                "agent": self.agent_name,
                "input_summary": input_summary,
                "output_summary": output_summary,
                "outcome": "pending",
            })
        except Exception:
            logger.warning(
                "EvolutionWrapper: failed to record episode for agent '%s'",
                self.agent_name,
                exc_info=True,
            )

        return result

    @staticmethod
    def _extract_summary(result: dict) -> str:
        """Best-effort extraction of a short summary from a node's return dict."""
        # Tool-using analysts return {"market_report": "...", ...}
        # Non-tool agents return {"messages": [...], ...}
        for key in _REPORT_KEYS:
            val = result.get(key)
            if val and isinstance(val, str):
                return val[:500]

        # Fallback: first message content
        msgs = result.get("messages")
        if msgs:
            last = msgs[-1]
            content = getattr(last, "content", str(last))
            if isinstance(content, str):
                return content[:500]
        return "(no summary extracted)"
