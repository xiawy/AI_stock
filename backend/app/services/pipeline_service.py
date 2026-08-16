"""Pipeline service — bridges FastAPI lifecycle with the impact pipeline.

Manages:
- LLM client creation for the pipeline
- APScheduler lifecycle (start/stop with FastAPI lifespan)
- Manual trigger endpoint support
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
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
        """Manually trigger a pipeline run.

        Routes through the scheduler so manual and scheduled runs share the
        same re-entry lock and failure tracking.
        """
        if not self._initialized or self._llm_quick is None:
            return {"status": "failed", "error": "Pipeline not initialized"}

        if self._scheduler is None:
            from ai_stock.pipeline.pipeline import run_full_pipeline
            from ai_stock.default_config import DEFAULT_CONFIG
            return run_full_pipeline(DEFAULT_CONFIG, self._llm_quick, self._llm_deep)

        return self._scheduler.trigger_manual()

    def ensure_today_data(self) -> None:
        """Kick off a pipeline run if today's ranking is missing.

        - Before the first scheduled slot (08:00 local) the previous day's
          snapshot is served as today's ranking, so no run is started.
        - From the first slot onwards, a missing snapshot (e.g. the backend
          started after the scheduled run) triggers an immediate background run.
        """
        if not self._initialized or self._llm_quick is None:
            return
        if self.is_running:
            return

        now = datetime.now()
        slots = self._scheduler.schedule_slots if self._scheduler else []
        first_slot = min(slots) if slots else (8, 0)
        if (now.hour, now.minute) < first_slot:
            logger.info(
                "Before first pipeline slot %02d:%02d; serving previous day's ranking",
                first_slot[0], first_slot[1],
            )
            return

        today = now.strftime("%Y-%m-%d")
        try:
            from ai_stock.pipeline.db_ops import snapshot_exists_for_date
            if snapshot_exists_for_date(today):
                return
        except Exception as exc:
            logger.warning("Snapshot check failed (%s); triggering run anyway", exc)

        logger.info("No snapshot for today %s; starting bootstrap pipeline run", today)
        threading.Thread(
            target=self._run_bootstrap, name="pipeline-bootstrap", daemon=True,
        ).start()

    def _run_bootstrap(self) -> None:
        """Run the pipeline in a background thread (startup bootstrap)."""
        try:
            result = self.run_pipeline()
            logger.info(
                "Bootstrap pipeline run finished: status=%s", result.get("status"),
            )
        except Exception as exc:
            logger.error("Bootstrap pipeline run failed: %s", exc)

    def get_latest(self) -> Optional[dict]:
        """Get the latest pipeline results."""
        from ai_stock.pipeline.db_ops import get_latest_snapshot
        return get_latest_snapshot()

    def get_by_date(self, date_str: str) -> Optional[dict]:
        """Get pipeline results for a specific date."""
        from ai_stock.pipeline.db_ops import get_snapshot_by_date
        return get_snapshot_by_date(date_str)

    def get_industry_latest(self) -> Optional[dict]:
        """Get the latest industry board (行业榜)."""
        from ai_stock.pipeline.db_ops import get_latest_industry_rankings
        return get_latest_industry_rankings()

    def get_industry_by_date(self, date_str: str) -> Optional[dict]:
        """Get industry board for a specific date."""
        from ai_stock.pipeline.db_ops import get_industry_rankings_by_date
        return get_industry_rankings_by_date(date_str)

    @property
    def is_running(self) -> bool:
        """True while a pipeline run is executing (shared with the scheduler)."""
        return self._scheduler.is_running if self._scheduler else False


# Module-level singleton
_service: Optional[PipelineService] = None


def get_pipeline_service() -> PipelineService:
    """Return the singleton pipeline service."""
    global _service
    if _service is None:
        _service = PipelineService()
    return _service
