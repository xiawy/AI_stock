"""Review engine: periodic review of agent episodes to generate learnings.

Reads all resolved episodes for an agent, splits them into successes and
failures, then asks the LLM to generate a summary and improvement suggestions.
Results are written to ``learnings/{agent_name}/{date}_summary.md``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, List

from .memory_system import AgentMemorySystem

logger = logging.getLogger(__name__)


class ReviewEngine:
    """Periodic review: reads episodes, generates learnings and improvement suggestions."""

    def __init__(
        self,
        agent_name: str,
        memory: AgentMemorySystem,
        llm: Any,
        learnings_dir: Path,
    ) -> None:
        self.agent_name = agent_name
        self.memory = memory
        self.llm = llm
        self._learnings_dir = Path(learnings_dir) / agent_name
        self._learnings_dir.mkdir(parents=True, exist_ok=True)

    def run_review(self) -> dict:
        """Generate personal summary and improvement suggestions.

        Returns a dict with ``summary`` (str), ``suggestions`` (str) and
        episode statistics (total/resolved/pending/successes/failures).
        Also writes the summary to the learnings directory when there is
        anything to review.

        Episodes with ``outcome == "pending"`` have not been verified against
        the market outcome yet. If no verified episodes exist, the review
        degrades to analysing the pending (unverified) episodes so the flow
        still produces actionable suggestions instead of a dead end.
        """
        episodes = self.memory.episodic.load_all()
        resolved = [e for e in episodes if e.get("outcome") != "pending"]
        pending = [e for e in episodes if e.get("outcome") == "pending"]

        stats = {
            "episodes_total": len(episodes),
            "episodes_resolved": len(resolved),
            "episodes_pending": len(pending),
        }

        if not episodes:
            msg = f"No episodes for agent '{self.agent_name}'. Skipping review."
            logger.info(msg)
            return {"summary": msg, "suggestions": "", **stats, "successes": 0, "failures": 0}

        # 结局未回填时（全部 pending），退化为对全部情节做模式复盘，
        # 保证人工审核流程在没有 outcome 数据时也能产出可用建议。
        pool = resolved or pending
        successes = [e for e in pool if self._is_success(e)]
        failures = [e for e in pool if not self._is_success(e)]
        unverified = not bool(resolved)

        summary = self._generate_summary(successes, failures, unverified=unverified)
        suggestions = self._generate_suggestions(failures, unverified=unverified)

        self._write_summary(summary, suggestions)

        return {
            "summary": summary,
            "suggestions": suggestions,
            **stats,
            "successes": len(successes),
            "failures": len(failures),
        }

    @staticmethod
    def _is_success(episode: dict) -> bool:
        """Determine if an episode was a success based on its outcome."""
        outcome = episode.get("outcome", "").lower()
        rating = episode.get("rating", "").lower()
        # Success if outcome is explicitly positive, or if rating is Buy/Overweight
        # and the stock went up (raw_return > 0)
        if outcome in ("success", "correct", "positive"):
            return True
        if outcome in ("failure", "incorrect", "negative"):
            return False
        # Heuristic: if rating is bullish and outcome contains "up", success
        if rating in ("buy", "overweight") and "up" in outcome:
            return True
        if rating in ("sell", "underweight") and "down" in outcome:
            return True
        return False

    def _generate_summary(self, successes: List[dict], failures: List[dict], unverified: bool = False) -> str:
        """Ask LLM to generate a review summary."""
        success_briefs = [self._brief(e) for e in successes[:10]]
        failure_briefs = [self._brief(e) for e in failures[:10]]

        note = ""
        if unverified:
            note = (
                "\n\nNote: these episodes have not yet been verified against actual "
                "market outcomes (outcome=pending). Treat the observed patterns as "
                "hypotheses to verify, not confirmed results."
            )

        prompt = f"""You are reviewing the past performance of the "{self.agent_name}" agent in an A-share stock analysis system.

**Successful analyses** ({len(successes)} total, showing up to 10):
{chr(10).join(success_briefs) or "(none)"}

**Failed analyses** ({len(failures)} total, showing up to 10):
{chr(10).join(failure_briefs) or "(none)"}{note}

Generate a concise review summary (in Chinese) covering:
1. Overall hit rate and pattern observations
2. What went well in successful cases
3. Common mistakes in failed cases
4. Key lessons for future analyses

Keep it under 500 words."""

        try:
            response = self.llm.invoke(prompt)
            return response.content
        except Exception:
            logger.warning("LLM summary generation failed for '%s'", self.agent_name, exc_info=True)
            return f"(Summary generation failed for {self.agent_name})"

    def _generate_suggestions(self, failures: List[dict], unverified: bool = False) -> str:
        """Generate improvement suggestions based on failures."""
        if not failures:
            return "No failures to learn from."

        failure_briefs = [self._brief(e) for e in failures[:10]]
        note = ""
        if unverified:
            note = (
                "\n\nNote: these episodes have not yet been verified against actual "
                "market outcomes (outcome=pending); still, highlight the most common "
                "weaknesses as candidate improvements."
            )
        prompt = f"""You are improving the "{self.agent_name}" agent in an A-share stock analysis system.

**Recent failed analyses** (showing up to 10):
{chr(10).join(failure_briefs)}{note}

Based on these failures, suggest up to 5 concrete, actionable improvements to the agent's analysis strategy (in Chinese). Each suggestion should be a single sentence.

Format as a numbered list."""

        try:
            response = self.llm.invoke(prompt)
            return response.content
        except Exception:
            logger.warning("LLM suggestions failed for '%s'", self.agent_name, exc_info=True)
            return "(Suggestion generation failed)"

    def _write_summary(self, summary: str, suggestions: str) -> None:
        """Write the review summary to the learnings directory."""
        date_str = datetime.now().strftime("%Y-%m-%d")
        filepath = self._learnings_dir / f"{date_str}_summary.md"

        content = f"""# {self.agent_name} Review Summary — {date_str}

## Summary

{summary}

## Improvement Suggestions

{suggestions}
"""
        filepath.write_text(content, encoding="utf-8")
        logger.info("Wrote review summary to %s", filepath)

    @staticmethod
    def _brief(episode: dict) -> str:
        """One-line summary of an episode."""
        ticker = episode.get("ticker", "?")
        date = episode.get("date", "?")
        outcome = episode.get("outcome", "?")
        rating = episode.get("rating", "?")
        output = episode.get("output_summary", "")[:100]
        return f"- [{ticker} {date}] rating={rating} outcome={outcome} | {output}"
