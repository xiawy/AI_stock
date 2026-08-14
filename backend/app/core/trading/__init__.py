"""Bridge to the original project's analysis engine.

Migration strategy (渐进式迁移): instead of copying the whole
``ai_stock/`` + ``web/`` + ``cli/`` packages into the backend, we keep a
single source of truth and import them from the original project root via
``sys.path``. The FastAPI backend reuses:

- ``ai_stock.graph.trading_graph.TradingAgentsGraph`` — the multi-agent pipeline
- ``web.runner.run_analysis_in_thread`` — background thread runner
- ``web.progress.ProgressTracker`` — thread-safe progress state
- ``web.history`` — completed / incomplete analysis management
- ``web.stock_display`` — "code + name" normalization for reports
- ``web.pdf_export`` — markdown / PDF export

All engine imports are **lazy** (per-call): the API process boots and serves
auth / docs even when the heavy analysis dependencies are not installed.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from app.core.config import get_settings


@lru_cache
def _project_root() -> Path:
    root = Path(get_settings().project_root).resolve()
    if not (root / "ai_stock").is_dir():
        raise RuntimeError(
            f"Analysis engine not found under {root} "
            "(expected ai_stock/ package; check PROJECT_ROOT setting)."
        )
    return root


def bootstrap() -> None:
    """Make the original project packages importable. Idempotent."""
    root = str(_project_root())
    if root not in sys.path:
        sys.path.insert(0, root)
