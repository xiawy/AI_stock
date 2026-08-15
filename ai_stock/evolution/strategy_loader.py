"""Custom strategy loader.

Reads user-editable Markdown files from ``custom_strategies/{agent_name}/``
and concatenates them into a single context string that gets injected into
the agent's system prompt.

Users drop ``.md`` files into the agent's subdirectory; the loader picks them
all up sorted by filename.  An empty or missing directory produces an empty
string, which the wrapper treats as "no custom strategy".
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


class StrategyLoader:
    """Loads user-editable .md strategy files for an agent."""

    def __init__(self, agent_name: str, strategies_dir: Path) -> None:
        self.agent_name = agent_name
        self._dir = Path(strategies_dir) / agent_name

    def load_all(self) -> str:
        """Concatenate all .md files in the agent's strategy directory.

        Returns a combined Markdown string, or empty string if the directory
        doesn't exist or contains no .md files.
        """
        if not self._dir.is_dir():
            return ""

        md_files: List[Path] = sorted(self._dir.glob("*.md"))
        if not md_files:
            return ""

        parts = []
        for fp in md_files:
            try:
                text = fp.read_text(encoding="utf-8").strip()
                if text:
                    parts.append(text)
            except OSError:
                logger.warning("Could not read strategy file: %s", fp)

        if not parts:
            return ""

        header = f"## Custom Strategies: {self.agent_name}\n"
        return header + "\n\n---\n\n".join(parts)

    def list_files(self) -> List[str]:
        """Return the filenames of all loaded strategy files."""
        if not self._dir.is_dir():
            return []
        return [p.name for p in sorted(self._dir.glob("*.md"))]
