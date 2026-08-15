"""Experience store: JSON files for human readability + Chroma for retrieval.

Every analysis run records an "episode" — a snapshot of what the agent saw,
what it concluded, and (later) whether the call was correct.  JSON files in
``evolution_data/{agent}/episodes/`` keep the data inspectable; the parallel
Chroma index powers similarity search for the memory system.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .vector_store import EpisodicVectorStore

logger = logging.getLogger(__name__)


class ExperienceStore:
    """Records analysis episodes per agent.

    JSON for readability, Chroma for retrieval.
    """

    def __init__(self, agent_name: str, base_dir: Path, chroma_client=None) -> None:
        self.agent_name = agent_name
        self._episodes_dir = Path(base_dir) / agent_name / "episodes"
        self._episodes_dir.mkdir(parents=True, exist_ok=True)

        self._vector = EpisodicVectorStore(
            agent_name,
            Path(base_dir) / agent_name / "chroma",
            client=chroma_client,
        )

    def record(self, episode: dict) -> None:
        """Write an episode to both JSON storage and the Chroma index."""
        # Ensure required fields
        episode.setdefault("id", str(uuid.uuid4()))
        episode.setdefault("agent", self.agent_name)
        episode.setdefault("outcome", "pending")
        episode.setdefault("recorded_at", datetime.now().isoformat())
    
        # Dedup: skip if this episode was already recorded (same id → same run)
        filename = f"{episode['id']}.json"
        filepath = self._episodes_dir / filename
        if filepath.exists():
            logger.debug("Episode %s already exists, skipping", episode["id"])
            return
    
        # Write JSON
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(episode, f, ensure_ascii=False, indent=2)
    
        # Write to Chroma
        try:
            self._vector.add_episode(episode)
        except Exception:
            logger.warning(
                "Failed to add episode %s to Chroma for agent '%s'",
                episode["id"],
                self.agent_name,
                exc_info=True,
            )
    
        logger.debug("Recorded episode %s for agent '%s'", episode["id"], self.agent_name)

    def load_all(self) -> List[dict]:
        """Read all JSON episode files from the episodes directory."""
        episodes = []
        for fp in sorted(self._episodes_dir.glob("*.json")):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    episodes.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                logger.warning("Skipping corrupt episode file: %s", fp)
        return episodes

    def retrieve_similar(self, query: str, n: int = 5) -> List[dict]:
        """Retrieve the most similar past episodes via Chroma."""
        return self._vector.retrieve(query, n_results=n)

    def update_outcome(self, episode_id: str, outcome: str, rating: Optional[str] = None) -> None:
        """Update the outcome (and optionally rating) of an existing episode.

        Modifies the JSON file in place. Chroma metadata is not updated (Chroma
        does not support in-place metadata updates without re-indexing).
        """
        filepath = self._episodes_dir / f"{episode_id}.json"
        if not filepath.exists():
            logger.warning("Episode %s not found for update", episode_id)
            return

        with open(filepath, "r", encoding="utf-8") as f:
            episode = json.load(f)

        episode["outcome"] = outcome
        if rating:
            episode["rating"] = rating

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(episode, f, ensure_ascii=False, indent=2)
