"""Three-layer memory system for a single agent.

Layers
------
- **Semantic memory** — user-editable strategy rules loaded from Markdown files.
  Changes only when the user edits the files.
- **Episodic memory** — timestamped analysis episodes stored as JSON + Chroma
  vector index.  Grows with every analysis run.
- **Working memory** — per-session ephemeral context.  Cleared when the
  process exits.

The main entry point is :meth:`build_evolution_context`, which assembles a
context string from semantic + episodic layers for injection into the agent's
system prompt.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List

from .experience_store import ExperienceStore
from .strategy_loader import StrategyLoader

logger = logging.getLogger(__name__)


def _format_context(strategies: str, similar_episodes: List[dict]) -> str:
    """Format strategies + similar episodes into a single context block."""
    parts = []

    if strategies:
        parts.append(strategies)

    if similar_episodes:
        lines = ["## Relevant Past Episodes\n"]
        for i, ep in enumerate(similar_episodes, 1):
            ticker = ep.get("ticker", "?")
            date = ep.get("date", "?")
            outcome = ep.get("outcome", "pending")
            doc = ep.get("document", "")
            # Truncate long documents for context window economy
            if len(doc) > 300:
                doc = doc[:300] + "..."
            lines.append(f"{i}. [{ticker} {date}] (outcome: {outcome}) {doc}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


class AgentMemorySystem:
    """Three-layer memory for a single agent."""

    def __init__(
        self,
        agent_name: str,
        base_dir: Path,
        strategies_dir: Path,
        top_k: int = 3,
        chroma_client=None,
    ) -> None:
        self.agent_name = agent_name
        self.semantic = StrategyLoader(agent_name, strategies_dir)
        self.episodic = ExperienceStore(agent_name, base_dir, chroma_client=chroma_client)
        self.working: Dict[str, str] = {}
        self._top_k = top_k

    def build_evolution_context(self, ticker: str, trade_date: str) -> str:
        """Build the context string to inject into the agent's system prompt.

        Combines:
        1. Custom strategies (semantic memory) from .md files
        2. Top-K similar past episodes (episodic memory) via Chroma
        3. Any working memory entries set during this session

        Returns an empty string when there is nothing to inject — the wrapper
        checks for this and skips prompt modification entirely.
        """
        # 1. Semantic: custom strategy rules
        strategies = self.semantic.load_all()

        # 2. Episodic: similar past analyses
        query = f"{ticker} analysis on {trade_date}"
        similar = self.episodic.retrieve_similar(query, n=self._top_k)

        # 3. Working: per-session context (if any)
        working_ctx = ""
        if self.working:
            working_parts = [f"- {k}: {v}" for k, v in self.working.items()]
            working_ctx = "## Session Notes\n" + "\n".join(working_parts)

        # Assemble
        all_parts = [p for p in [strategies, working_ctx] if p]
        context = _format_context(
            "\n\n".join(all_parts) if all_parts else "",
            similar,
        )

        return context

    def set_working(self, key: str, value: str) -> None:
        """Set a working memory entry for the current session."""
        self.working[key] = value

    def clear_working(self) -> None:
        """Clear all working memory entries."""
        self.working.clear()
