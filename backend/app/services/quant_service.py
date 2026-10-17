"""Quant trading subsystem bridge — start/stop/status for the FastAPI app.

The quant engine lives in ``ai_stock.quant`` (single source of truth, see
``app.core.trading`` for the same progressive-migration strategy). This
wrapper is the only place the backend touches it:

- ``start_quant_service`` / ``stop_quant_service`` — wired into the app
  lifespan (``app.main``); failure is non-fatal, the rest of the API keeps
  serving.
- ``prepare_quant_env`` — pins ``PROJECT_ROOT`` before any engine import so
  the quant DB/cache land in the shared project data directory.

Import discipline: all engine imports are lazy (inside functions), matching
the convention used by pipeline/evolution services — the API process boots
even if quant dependencies are missing.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def prepare_quant_env() -> None:
    """Pin PROJECT_ROOT (engine data directory anchor) before engine import.

    Root ``.env`` usually provides it (loaded by ``app.core.config``); this
    is the fallback so ``ai_stock.quant.config`` never falls back to
    ``~/.ai_stock`` when running under the backend.
    """
    from app.core.config import PROJECT_ROOT

    os.environ.setdefault("PROJECT_ROOT", str(PROJECT_ROOT))


def start_quant_service():
    """Start the quant service (consumers + scheduler). Idempotent.

    多 worker 部署（``uvicorn --workers N``）下只有**领导者** worker 启动交易
    子系统：消费者/调度器/清扫器都必须单实例，否则 N 份调度器并发发任务。
    其余 worker 照常提供 API（含交易查询），领导者进程退出后内核释放文件锁，
    由 uvicorn 拉起的新 worker 接手。

    Returns the running service, or ``None`` when startup was skipped/failed —
    callers treat quant as optional and must not crash the app on its failure.
    """
    try:
        prepare_quant_env()
        from ai_stock.quant import leader

        if not leader.acquire():
            logger.info(
                "Quant subsystem owned by another worker; this worker serves API only"
            )
            return None

        from ai_stock.quant import get_quant_service

        svc = get_quant_service()
        if svc.started:
            return svc
        svc.start()
        logger.info("Quant service started (consumers + scheduler)")
        return svc
    except Exception as exc:
        logger.warning("Quant service start failed: %s", exc)
        return None


def stop_quant_service() -> None:
    """Stop the quant service if it was started. Never raises."""
    try:
        from ai_stock.quant.service import get_quant_service

        svc = get_quant_service()
        if svc.started:
            svc.stop()
            logger.info("Quant service stopped")
    except Exception as exc:
        logger.warning("Quant service stop failed: %s", exc)
    finally:
        try:
            from ai_stock.quant import leader

            leader.release()
        except Exception:  # pragma: no cover - 释放失败不影响退出
            pass


def get_quant_status() -> dict:
    """Service/runtime view for the admin API (never raises)."""
    try:
        from ai_stock.quant.service import get_quant_service

        svc = get_quant_service()
        return svc.status()
    except Exception as exc:
        return {"running": False, "error": str(exc)}
