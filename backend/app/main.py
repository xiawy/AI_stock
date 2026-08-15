"""AI Stock — FastAPI application entry point.

Run (dev):  cd backend && .venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import analysis, auth, history, impact, recommendation, stocks, watchlist
from app.core.config import get_settings
from app.core.database import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Create SQLite tables on first start; Alembic owns later migrations.
    init_db()

    # Initialize the impact pipeline service (scheduler + LLM clients).
    try:
        from ai_stock.default_config import DEFAULT_CONFIG
        from app.services.pipeline_service import get_pipeline_service

        svc = get_pipeline_service()
        svc.initialize(DEFAULT_CONFIG)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
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
