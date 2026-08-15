

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


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Create SQLite tables on first start; Alembic owns later migrations.
    init_db()

    _reap_orphaned_tasks()

    # Initialize the impact pipeline service (scheduler + LLM clients).
    try:
        from ai_stock.default_config import DEFAULT_CONFIG
        from app.services.pipeline_service import get_pipeline_service

        svc = get_pipeline_service()
        svc.initialize(DEFAULT_CONFIG)
    except Exception as exc:
        logger.warning(
            "Pipeline service init failed (non-fatal): %s", exc,
        )

    yield

    # Shutdown pipeline scheduler
    try:
        from app.services.pipeline_service import get_pipeline_service
        get_pipeline_service().shutdown()
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
