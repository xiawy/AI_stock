

"""AI Stock — FastAPI application entry point.

Run (dev):  cd backend && .venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import analysis, auth, history, impact, recommendation, stocks, watchlist
from app.core.config import get_settings
from app.core.database import init_db

logger = logging.getLogger(__name__)


def _reap_orphaned_tasks() -> None:
    """Freeze tasks left running/paused by a previous process.

    Trackers live in process memory; after a restart no tracker exists for
    those rows, so ``sync_task_row`` would never refresh them and the UI
    would show them as alive forever. Single-worker deployments only
    (the in-memory registry is not shared across workers anyway).
    """
    from datetime import datetime, timezone

    from sqlalchemy import update

    from app.core.database import SessionLocal
    from app.models.analysis_task import AnalysisTask

    with SessionLocal() as session:
        result = session.execute(
            update(AnalysisTask)
            .where(AnalysisTask.status.in_(["running", "paused"]))
            .values(
                status="stopped",
                error="服务重启，任务已中断；可从历史记录重新发起分析",
                updated_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
        if result.rowcount:
            logger.warning(
                "Reaped %d orphaned analysis task(s) left running/paused by a previous process",
                result.rowcount,
            )


def _reap_orphaned_snapshots() -> None:
    """Mark impact-pipeline snapshots left ``running`` by a previous process
    as ``failed``.

    Pipeline runs execute in daemon threads; a backend restart mid-run kills
    the thread and freezes the snapshot in ``running`` forever. Those rows are
    invisible to ``get_latest_snapshot`` (completed only) but keep today's
    ``ensure_today_data`` bootstrap from ever being considered done, so the
    rankings would stay empty until the next scheduled slot.
    """
    from sqlalchemy import update

    from ai_stock.pipeline.db_models import ImpactSnapshot
    from app.core.database import SessionLocal

    with SessionLocal() as session:
        result = session.execute(
            update(ImpactSnapshot)
            .where(ImpactSnapshot.status == "running")
            .values(status="failed")
        )
        session.commit()
        if result.rowcount:
            logger.warning(
                "Reaped %d orphaned impact snapshot(s) left running by a previous process",
                result.rowcount,
            )


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Create SQLite tables on first start; Alembic owns later migrations.
    init_db()

    _reap_orphaned_tasks()
    _reap_orphaned_snapshots()

    # Data retention: 诊股 > 20 days / 寻龙榜·新闻榜 > 70 days. One pass now
    # (background thread) and daily at 03:30 afterwards.
    try:
        from app.services.cleanup import (
            start_cleanup_scheduler,
            start_initial_cleanup,
        )

        start_initial_cleanup()
        start_cleanup_scheduler()
    except Exception as exc:
        logger.warning("Cleanup scheduler init failed (non-fatal): %s", exc)

    # Initialize the impact pipeline service (scheduler + LLM clients).
    try:
        from ai_stock.default_config import DEFAULT_CONFIG
        from app.services.pipeline_service import get_pipeline_service

        svc = get_pipeline_service()
        svc.initialize(DEFAULT_CONFIG)
        # If today's ranking is missing (backend started after the scheduled
        # slot), kick off an immediate background run. Before the first slot
        # the previous day's snapshot is served as today's ranking.
        svc.ensure_today_data()
    except Exception as exc:
        logger.warning(
            "Pipeline service init failed (non-fatal): %s", exc,
        )

    yield

    # Shutdown pipeline scheduler + cleanup scheduler
    try:
        from app.services.pipeline_service import get_pipeline_service
        get_pipeline_service().shutdown()
    except Exception:
        pass
    try:
        from app.services.cleanup import stop_cleanup_scheduler
        stop_cleanup_scheduler()
    except Exception:
        pass


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="AI Stock 前后端分离 API — A股多Agent投研分析系统",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health", tags=["meta"], summary="健康检查")
def health() -> dict:
    return {"status": "ok", "app": settings.app_name}


app.include_router(auth.router, prefix="/api")
app.include_router(analysis.router, prefix="/api")
app.include_router(stocks.router, prefix="/api")
app.include_router(history.router, prefix="/api")
app.include_router(watchlist.router, prefix="/api")
app.include_router(impact.router, prefix="/api")
app.include_router(recommendation.router, prefix="/api")
