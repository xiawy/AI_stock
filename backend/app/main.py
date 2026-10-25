

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
    board,
    evolution,
    history,
    impact,
    industry,
    quant,
    recommendation,
    stocks,
    strategy,
    watchlist,
)
from app.core.config import get_settings
from app.core.database import init_db

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """让应用日志（ai_stock.* / app.*）在 INFO 级别可见。

    uvicorn 只配置自己的 logger，应用 logger 会冒泡到 root；而 root 默认
    WARNING，于是「Quant service started」「Consumer xxx started」这类关键启动
    日志被静默吞掉 —— 现场排障时极易误判成"调度器没起来"（本次上线踩过）。
    uvicorn 自身 logger 的 propagate=False，不会重复输出。

    LOG_LEVEL 可用环境变量覆盖（DEBUG/INFO/WARNING...），默认 INFO。
    """
    level_name = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger().setLevel(level)


_configure_logging()


def _reap_orphaned_tasks() -> None:
    """回收「心跳已过期」的 running/paused 任务行。

    A5 之后判定依据是 ``heartbeat_at`` 而非"启动即冻结"：多 worker 部署下
    其它 worker 上活着的任务心跳新鲜，不能被本进程启动时误杀。真正的孤儿
    （上个进程已退出）会在心跳阈值过后由启动巡检与周期巡检统一回收。
    """
    try:
        from app.services.analysis_service import task_manager

        task_manager.reap_stale()
    except Exception as exc:  # pragma: no cover - 回收失败不应阻断启动
        logger.warning("Reap orphaned analysis tasks failed (non-fatal): %s", exc)


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

    if not _background_jobs_enabled():
        logger.info("Background jobs disabled via AISTOCK_DISABLE_SCHEDULERS")
        yield
        return

    # A5: 心跳线程（顺带做孤儿巡检）。任何 worker 起的任务都要能被别人看到，
    # 因此每个 worker 都跑；它同时负责消费跨进程 pause/resume/stop 指令。
    try:
        from app.services.analysis_service import task_manager

        task_manager.start_heartbeat()
    except Exception as exc:
        logger.warning("Analysis task heartbeat init failed (non-fatal): %s", exc)

    # ── 领导者选举必须先跑 ────────────────────────────────────────────
    # 多 worker（uvicorn --workers N）时，只有抢到文件锁的 worker 启动交易
    # 子系统与各类后台调度；其余 worker 只服务 API。下面 cleanup / pipeline /
    # evolution 一律用 leader.is_leader() 兜住，否则 N 个 worker 各跑一份
    # 清理、备份和学习调度 —— 幂等不代表无害，重复清扫会互相抢锁、重复备份
    # 会覆盖文件、学习调度会重复消耗 LLM。
    try:
        if settings.quant_enabled:
            from app.services.quant_service import start_quant_service

            start_quant_service()
        else:
            logger.info("Quant service disabled via QUANT_ENABLED")
    except Exception as exc:
        logger.warning("Quant service init failed (non-fatal): %s", exc)

    def _is_leader() -> bool:
        """单 worker 部署下永远为 True；多 worker 下仅领导者为 True。"""
        try:
            from ai_stock.quant import leader

            # 未启用量化时没人抢锁，此时退化为「全部允许」。
            return leader.is_leader() or not settings.quant_enabled
        except Exception:
            return True

    leader_mode = _is_leader()
    if not leader_mode:
        logger.info("This worker serves API only (background jobs owned by leader)")

    # 孤儿回收只在领导者里跑：多个 worker 同时扫同一个 running 任务会把
    # 别人的活判成超时（心跳列还没来得及刷新），A5 状态机会来回跳。
    if leader_mode:
        _reap_orphaned_tasks()
        _reap_orphaned_snapshots()

    # Data retention: 诊股 > 20 days / 热股榜·新闻榜 > 70 days. One pass now
    # (background thread) and daily at 03:30 afterwards.
    if leader_mode:
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
    if leader_mode:
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
            if not leader_mode:
                # 学习调度会调 LLM 生成复盘，多个 worker 各跑一份既费钱又会
                # 生成互相矛盾的草稿。
                logger.info("Evolution scheduler skipped on non-leader worker")
            # The volatility trigger needs a market-data callback; keep the
            # periodic review job only for now (schedule lives in config).
            sched_cfg = {**DEFAULT_CONFIG, "review_volatility_trigger": False}
            evo_svc = get_evolution_service()
            evo_scheduler = None if not leader_mode else EvolutionScheduler(
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
            if evo_scheduler is not None:
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
app.include_router(strategy.router, prefix="/api")
app.include_router(board.router, prefix="/api")
