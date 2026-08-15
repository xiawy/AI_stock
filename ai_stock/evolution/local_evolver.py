"""Local evolver: generates strategy modification drafts based on review.

IMPORTANT: Does NOT auto-apply changes. Generates a draft that goes into a
review queue. The user must explicitly approve before any strategy file is
modified.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class LocalEvolver:
    """Generates strategy modification suggestions based on review.

    All changes go through a review queue — nothing is auto-applied.
    """

    def __init__(
        self,
        agent_name: str,
        llm: Any,
        strategies_dir: Path,
        review_queue_dir: Path,
    ) -> None:
        self.agent_name = agent_name
        self.llm = llm
        self._strategies_dir = Path(strategies_dir) / agent_name
        self._queue_dir = Path(review_queue_dir) / agent_name
        self._queue_dir.mkdir(parents=True, exist_ok=True)

    def generate_draft(self, current_strategy: str, suggestions: str) -> str:
        """Ask LLM to propose a strategy modification.

        Returns the markdown draft content. The draft is saved to the review
        queue directory — the user must call ``apply_approved`` to activate it.
        """
        prompt = f"""You are improving the analysis strategy for the "{self.agent_name}" agent in an A-share stock analysis system.

**Current strategy:**
{current_strategy}

**Review suggestions for improvement:**
{suggestions}

Generate an updated version of the strategy that incorporates the suggestions. Keep the same Markdown format. Only modify what the suggestions indicate — preserve everything else unchanged.

Output the complete updated strategy file content (in Chinese, matching the original format)."""

        try:
            response = self.llm.invoke(prompt)
            draft = response.content
        except Exception:
            logger.warning("Draft generation failed for '%s'", self.agent_name, exc_info=True)
            return ""

        # Save to review queue
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        draft_path = self._queue_dir / f"draft_{timestamp}.md"
        draft_path.write_text(draft, encoding="utf-8")
        logger.info("Strategy draft saved to %s (pending review)", draft_path)

        return draft

    def apply_approved(self, draft_path: Path) -> None:
        """Move an approved draft to the active strategies directory.

        The draft replaces the current strategy files. A backup of the
        existing files is created first.
        """
        draft_path = Path(draft_path)
        if not draft_path.exists():
            raise FileNotFoundError(f"Draft not found: {draft_path}")

        # Backup current strategies
        if self._strategies_dir.is_dir():
            backup_dir = self._strategies_dir.with_name(
                f"{self._strategies_dir.name}_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
            shutil.copytree(self._strategies_dir, backup_dir)
            logger.info("Backed up current strategies to %s", backup_dir)

        # Clear and replace
        if self._strategies_dir.is_dir():
            for f in self._strategies_dir.glob("*.md"):
                f.unlink()
        else:
            self._strategies_dir.mkdir(parents=True, exist_ok=True)

        # Copy draft as the new strategy
        dest = self._strategies_dir / f"strategy_{datetime.now().strftime('%Y%m%d')}.md"
        shutil.copy2(draft_path, dest)
        logger.info("Applied approved draft: %s → %s", draft_path, dest)

    def list_drafts(self) -> list:
        """List all pending drafts in the review queue."""
        return sorted(self._queue_dir.glob("draft_*.md"))
