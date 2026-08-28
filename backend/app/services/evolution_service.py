"""Evolution review service — LLM + background job tracking for the review API.

Thin wrapper over ``ai_stock.evolution.review_service`` (shared with the CLI).
Review runs happen in daemon threads so the web UI can trigger a review and
poll ``GET /api/evolution/review/jobs/{job_id}`` instead of blocking a request
for the duration of the LLM calls.

Safety invariant: nothing here ever auto-applies a strategy change. The review
only writes learning summaries and *draft* files; ``approve_draft`` is the only
path that touches the active ``custom_strategies/`` tree (after backing it up).
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


class EvolutionReviewManager:
    """Manages review jobs and the review LLM for the backend."""

    def __init__(self) -> None:
        self._llm: Optional[Any] = None
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ── LLM ────────────────────────────────────────────────────────────

    def get_llm(self) -> Any:
        """Build (once) and return the LLM used for reviews."""
        if self._llm is None:
            from ai_stock.default_config import DEFAULT_CONFIG
            from ai_stock.evolution import review_service

            self._llm = review_service.build_llm(DEFAULT_CONFIG)
        return self._llm

    # ── Review jobs ────────────────────────────────────────────────────

    def start_review(self, agent_name: str, generate_draft: bool = True) -> str:
        """Kick off a background review for one agent; returns a job id."""
        from ai_stock.default_config import DEFAULT_CONFIG
        from ai_stock.evolution import review_service

        review_service.validate_agent(agent_name, DEFAULT_CONFIG)

        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "agent": agent_name,
            "status": "running",
            "created_at": _now_iso(),
            "finished_at": None,
            "result": None,
            "error": None,
        }
        with self._lock:
            self._jobs[job_id] = job

        threading.Thread(
            target=self._run_job,
            args=(job_id, agent_name, generate_draft),
            name=f"evolution-review-{agent_name}",
            daemon=True,
        ).start()
        return job_id

    def _run_job(self, job_id: str, agent_name: str, generate_draft: bool) -> None:
        from ai_stock.default_config import DEFAULT_CONFIG
        from ai_stock.evolution import review_service

        try:
            llm = self.get_llm()
            result = review_service.run_review_for_agent(
                agent_name,
                DEFAULT_CONFIG,
                llm=llm,
                generate_draft=generate_draft,
            )
            with self._lock:
                job = self._jobs[job_id]
                job["status"] = "success"
                job["finished_at"] = _now_iso()
                job["result"] = _jsonable(result)
        except Exception as exc:
            logger.error(
                "Review job %s failed for agent '%s': %s", job_id, agent_name, exc,
            )
            with self._lock:
                job = self._jobs[job_id]
                job["status"] = "failed"
                job["finished_at"] = _now_iso()
                job["error"] = str(exc)

    def get_job(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="复盘任务不存在或已过期（服务重启后任务记录会丢失）",
            )
        return job

    # ── Draft queue / status (delegate to the shared service) ──────────

    def list_drafts(self, agent_name: Optional[str] = None) -> list:
        from ai_stock.default_config import DEFAULT_CONFIG
        from ai_stock.evolution import review_service

        return review_service.list_drafts(DEFAULT_CONFIG, agent_name=agent_name)

    def get_draft(self, agent_name: str, filename: str) -> dict:
        from ai_stock.default_config import DEFAULT_CONFIG
        from ai_stock.evolution import review_service

        return _guard(review_service.get_draft, DEFAULT_CONFIG, agent_name, filename)

    def approve_draft(self, agent_name: str, filename: str) -> dict:
        from ai_stock.default_config import DEFAULT_CONFIG
        from ai_stock.evolution import review_service

        return _guard(review_service.approve_draft, DEFAULT_CONFIG, agent_name, filename)

    def reject_draft(self, agent_name: str, filename: str) -> dict:
        from ai_stock.default_config import DEFAULT_CONFIG
        from ai_stock.evolution import review_service

        return _guard(review_service.reject_draft, DEFAULT_CONFIG, agent_name, filename)


def _guard(fn, *args):
    """Map domain exceptions to HTTP errors."""
    try:
        return fn(*args)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _jsonable(value):
    """Best-effort strip of non-JSON values (e.g. Path) from a result dict."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "__fspath__"):
        return str(value)
    return value


_manager: Optional[EvolutionReviewManager] = None


def get_evolution_service() -> EvolutionReviewManager:
    """Return the singleton evolution review manager."""
    global _manager
    if _manager is None:
        _manager = EvolutionReviewManager()
    return _manager
