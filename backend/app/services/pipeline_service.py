"""Pipeline service — bridges FastAPI lifecycle with the impact pipeline.

Manages:
- LLM client creation for the pipeline
- APScheduler lifecycle (start/stop with FastAPI lifespan)
- Manual trigger endpoint support
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PipelineService:
    """Service layer wrapping the impact pipeline for the backend."""

    def __init__(self) -> None:
        self._llm_quick: Optional[Any] = None
        self._llm_deep: Optional[Any] = None
        self._scheduler = None
        self._initialized = False

    def initialize(self, config: dict) -> None:
        """Create LLM clients and start the scheduler.

        Called during FastAPI lifespan startup.
        """
        if self._initialized:
            return

        try:
            from ai_stock.llm_clients.factory import create_llm_client

            provider = config.get("llm_provider", "openai")
            quick_model = config.get("quick_think_llm", "gpt-5.4-mini")
            deep_model = config.get("deep_think_llm", "gpt-5.4")
            base_url = config.get("backend_url")

            llm_kwargs = {}
            max_tokens = config.get("max_tokens")
            if max_tokens:
                llm_kwargs["max_tokens"] = max_tokens

            quick_client = create_llm_client(
                provider=provider, model=quick_model,
                base_url=base_url, **llm_kwargs,
            )
            deep_client = create_llm_client(
                provider=provider, model=deep_model,
                base_url=base_url, **llm_kwargs,
            )

            self._llm_quick = quick_client.get_llm()
            self._llm_deep = deep_client.get_llm()

            # Start scheduler
            from ai_stock.pipeline.scheduler import create_scheduler
            self._scheduler = create_scheduler(config, self._llm_quick, self._llm_deep)
            self._scheduler.start()

            self._initialized = True
            logger.info("Pipeline service initialized (provider=%s)", provider)

        except Exception as exc:
            logger.error("Pipeline service initialization failed: %s", exc)

    def shutdown(self) -> None:
        """Stop the scheduler. Called during FastAPI lifespan shutdown."""
        if self._scheduler:
            self._scheduler.stop()
            self._scheduler = None
        self._initialized = False
        logger.info("Pipeline service shut down")

    def run_pipeline(self) -> dict:
        """Manually trigger a pipeline run."""
        if not self._initialized or self._llm_quick is None:
            return {"status": "failed", "error": "Pipeline not initialized"}

        from ai_stock.pipeline.pipeline import run_full_pipeline
        from ai_stock.default_config import DEFAULT_CONFIG

        return run_full_pipeline(DEFAULT_CONFIG, self._llm_quick, self._llm_deep)

    def get_latest(self) -> Optional[dict]:
        """Get the latest pipeline results."""
        from ai_stock.pipeline.db_ops import get_latest_snapshot
        return get_latest_snapshot()

    def get_by_date(self, date_str: str) -> Optional[dict]:
        """Get pipeline results for a specific date."""
        from ai_stock.pipeline.db_ops import get_snapshot_by_date
        return get_snapshot_by_date(date_str)

    @property
    def is_running(self) -> bool:
        return self._scheduler.is_running if self._scheduler else False


# Module-level singleton
_service: Optional[PipelineService] = None


def get_pipeline_service() -> PipelineService:
    """Return the singleton pipeline service."""
    global _service
    if _service is None:
        _service = PipelineService()
    return _service
