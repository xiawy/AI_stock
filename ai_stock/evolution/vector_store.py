"""Chroma-backed episodic vector store.

Each agent gets its own persistent Chroma collection so that episode retrieval
is scoped to the agent's own experience — market analyst episodes don't leak
into the hot-money analyst's context window.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


class EpisodicVectorStore:
    """Per-agent Chroma collection for episodic memory."""

    def __init__(self, agent_name: str, persist_dir: Path, client=None) -> None:
        self.agent_name = agent_name
        self._persist_dir = Path(persist_dir)
        self._persist_dir.mkdir(parents=True, exist_ok=True)

        if client is not None:
            # Reuse a shared PersistentClient (avoids loading the ONNX
            # embedding model once per agent — 15 agents × 1 model = waste).
            self._client = client
        else:
            try:
                import chromadb
            except ImportError:
                raise ImportError(
                    "chromadb is required for the evolution system. "
                    "Install it with: pip install 'chromadb>=0.5.0'"
                )
            self._client = chromadb.PersistentClient(path=str(self._persist_dir))

        self.collection = self._client.get_or_create_collection(
            name=f"{agent_name}_episodes",
            metadata={"hnsw:space": "cosine"},
        )
        logger.debug(
            "Chroma collection '%s_episodes' ready (%d existing docs)",
            agent_name,
            self.collection.count(),
        )

    def add_episode(self, episode: dict) -> None:
        """Add a single episode to the vector store.

        Expected episode keys:
            id (str), ticker, date, agent, input_summary, output_summary,
            outcome (optional, default "pending"), rating (optional).
        """
        doc_id = episode.get("id") or f"{episode['ticker']}_{episode['date']}_{episode['agent']}"
        # Dedup: skip if this exact id already exists in the collection
        existing = self.collection.get(ids=[doc_id])
        if existing and existing.get("ids"):
            return
        document = episode.get("input_summary", "") + " | " + episode.get("output_summary", "")
        metadata = {
            "ticker": str(episode.get("ticker", "")),
            "date": str(episode.get("date", "")),
            "agent": str(episode.get("agent", self.agent_name)),
            "outcome": str(episode.get("outcome", "pending")),
        }
        # Chroma metadata values must be str/int/float/bool
        if "rating" in episode:
            metadata["rating"] = str(episode["rating"])

        self.collection.add(
            documents=[document],
            metadatas=[metadata],
            ids=[doc_id],
        )

    def retrieve(self, query: str, n_results: int = 5) -> List[dict]:
        """Retrieve the most similar episodes for a given query string."""
        if self.collection.count() == 0:
            return []

        # Clamp n_results to the number of available documents
        n = min(n_results, self.collection.count())
        if n <= 0:
            return []

        results = self.collection.query(
            query_texts=[query],
            n_results=n,
        )

        episodes = []
        if results and results.get("ids") and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                ep = {
                    "id": doc_id,
                    "document": results["documents"][0][i] if results.get("documents") else "",
                }
                if results.get("metadatas") and results["metadatas"][0]:
                    ep.update(results["metadatas"][0][i])
                if results.get("distances") and results["distances"][0]:
                    ep["distance"] = results["distances"][0][i]
                episodes.append(ep)
        return episodes
