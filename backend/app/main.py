

"""AI Stock — FastAPI application entry point.

Run (dev):  cd backend && .venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    analysis,
    auth,
    evolution,
    history,
    impact,
    industry,
    quant,
    recommendation,
    stocks,
    watchlist,
)
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
    """Mark news-board snapshots left ``running`` by a previous process
    as ``failed``.

    新闻榜落库 (选股流程 MacroEventAgent 步) 先建 ``running`` 快照再写条目;
    进程在写入中途退出会把快照永远冻结在 ``running``。这些行对
    ``get_latest_snapshot`` (只读 completed) 不可见, 启动时统一标为 ``failed``。
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


def _background_jobs_enabled() -> bool:
    """False in test runs, where lifespan side effects (pipeline bootstrap,
    LLM clients, schedulers, cleanup threads) must not start."""
    return os.environ.get("AISTOCK_DISABLE_SCHEDULERS", "").lower() not in ("1", "true", "yes")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Create SQLite tables on first start; Alembic owns later migrations.
    init_db()

    _reap_orphaned_tasks()
    _reap_orphaned_snapshots()

    if not _background_jobs_enabled():
        logger.info("Background jobs disabled via AISTOCK_DISABLE_SCHEDULERS")
        yield
        return

    # Data retention: 诊股 > 20 days / 热股榜·新闻榜 > 70 days. One pass now
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

    # Initialize the ranking service (新闻榜由选股流程落库; 这里只管备份调度).
    try:
        from ai_stock.default_config import DEFAULT_CONFIG
        from app.services.pipeline_service import get_pipeline_service

        svc = get_pipeline_service()
        svc.initialize(DEFAULT_CONFIG)
        # Compensate for a missed 23:30 ranking backup (backend was down at
        # the slot): back up now if it is already past the slot and today's
        # backup file is absent.
        svc.ensure_today_backup()
    except Exception as exc:
        logger.warning(
            "Pipeline service init failed (non-fatal): %s", exc,
        )

    # Start the quant trading subsystem (V3.0): MQ consumers + scheduler +
    # FSM recovery. Ships with global_trade_enable=0 (simulation only) until
    # an operator flips the switch; failure is non-fatal for the API.
    try:
        if settings.quant_enabled:
            from app.services.quant_service import start_quant_service

            start_quant_service()
        else:
            logger.info("Quant service disabled via QUANT_ENABLED")
    except Exception as exc:
        logger.warning("Quant service init failed (non-fatal): %s", exc)

    # Start the evolution review scheduler. It only generates learning
    # summaries + strategy *drafts* on schedule — nothing is applied without
    # explicit human approval on the 进化审核 page / CLI.
    evo_scheduler = None
    try:
        from ai_stock.default_config import DEFAULT_CONFIG
        from ai_stock.evolution import review_service as evo_review
        from ai_stock.evolution.scheduler import EvolutionScheduler
        from app.services.evolution_service import get_evolution_service

        if DEFAULT_CONFIG.get("evolution_enabled", True):
            # The volatility trigger needs a market-data callback; keep the
            # periodic review job only for now (schedule lives in config).
            sched_cfg = {**DEFAULT_CONFIG, "review_volatility_trigger": False}
            evo_svc = get_evolution_service()
            evo_scheduler = EvolutionScheduler(
                agents=evo_review.AGENTS,
                config=sched_cfg,
                review_fn=lambda agent: evo_review.run_review_for_agent(
                    agent,
                    DEFAULT_CONFIG,
                    llm=evo_svc.get_llm(),
                    generate_draft=True,
                ),
                volatility_fn=None,
            )
            evo_scheduler.start()
        else:
            logger.info("Evolution disabled via config — review scheduler not started")
    except Exception as exc:
        evo_scheduler = None
        logger.warning("Evolution scheduler init failed (non-fatal): %s", exc)

    yield

    # Shutdown quant consumers/scheduler first (they own background threads)
    try:
        from app.services.quant_service import stop_quant_service

        stop_quant_service()
    except Exception:
        pass
    # Shutdown evolution scheduler first (its review_fn shares the LLM)
    try:
        if evo_scheduler is not None:
            evo_scheduler.stop()
    except Exception:
        pass
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
app.include_router(industry.router, prefix="/api")
app.include_router(recommendation.router, prefix="/api")
app.include_router(evolution.router, prefix="/api")
app.include_router(quant.router, prefix="/api")
