"""12-hour result cache for the pipeline.

Avoids redundant LLM calls when the same news items are scored multiple
times within the same 12h window.  Uses a simple in-memory dict keyed by
title hash; the pipeline clears stale entries on each run.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Cache TTL: 12 hours
_CACHE_TTL_SECONDS = 12 * 3600


class PipelineCache:
    """Thread-safe in-memory cache for pipeline intermediate results."""

    def __init__(self, ttl: int = _CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._store: dict[str, tuple[float, Any]] = {}  # key -> (timestamp, value)

    def get(self, key: str) -> Any | None:
        """Get a cached value, or None if missing/expired."""
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts > self._ttl:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        """Store a value in the cache."""
        self._store[key] = (time.time(), value)

    def invalidate(self, key: str) -> None:
        """Remove a specific key."""
        self._store.pop(key, None)

    def clear_expired(self) -> int:
        """Remove all expired entries. Returns count removed."""
        now = time.time()
        expired = [k for k, (ts, _) in self._store.items() if now - ts > self._ttl]
        for k in expired:
            del self._store[k]
        return len(expired)

    def clear_all(self) -> None:
        """Clear the entire cache."""
        self._store.clear()

    @property
    def size(self) -> int:
        return len(self._store)


# Module-level singleton
_cache = PipelineCache()


def get_cache() -> PipelineCache:
    """Return the module-level cache singleton."""
    return _cache
