"""Stock recommendation API endpoints.

GET  /api/recommendation/latest   — Latest Top 10 + 3 alternates
GET  /api/recommendation/history   — Historical query by date
POST /api/recommendation/trigger   — Manually trigger a pipeline run
"""

from __future__ import annotations

import threading

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from app.dependencies import get_current_user
from app.models import User
from app.services.pipeline_service import get_pipeline_service

router = APIRouter(prefix="/recommendation", tags=["recommendation"])


@router.get("/latest", summary="最新一期 Top 10 + 3 备选荐股")
def get_latest(
    current_user: User = Depends(get_current_user),
) -> dict:
    """Return the latest stock recommendations."""
    svc = get_pipeline_service()
    result = svc.get_latest()
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="暂无荐股数据，请先手动触发流水线",
        )
    # Return only the recommendations portion
    return {
        "snapshot": result.get("snapshot"),
        "recommendations": result.get("recommendations", []),
    }


@router.get("/history", summary="按日期查询荐股结果")
def get_history(
    date: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    """Query recommendations for a specific date (YYYY-MM-DD)."""
    svc = get_pipeline_service()
    result = svc.get_by_date(date)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到 {date} 的荐股数据",
        )
    return {
        "snapshot": result.get("snapshot"),
        "recommendations": result.get("recommendations", []),
    }


@router.post("/trigger", summary="手动触发一次流水线", status_code=status.HTTP_202_ACCEPTED)
def trigger_pipeline(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
) -> dict:
    """Trigger a manual pipeline run in the background."""
    svc = get_pipeline_service()

    if svc.is_running:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="流水线正在运行中，请稍后再试",
        )

    # Run in background thread to not block the API response
    background_tasks.add_task(_run_pipeline_async)

    return {
        "detail": "流水线已启动，请在后台等待完成",
        "scheduler_running": svc.is_running,
    }


def _run_pipeline_async() -> None:
    """Run the pipeline in a background thread."""
    svc = get_pipeline_service()
    try:
        result = svc.run_pipeline()
        if result.get("status") == "completed":
            from app.services.pipeline_service import logger
            logger.info(
                "Manual pipeline run completed: %d recommendations",
                len(result.get("recommendations", [])),
            )
    except Exception as exc:
        from app.services.pipeline_service import logger
        logger.error("Manual pipeline run failed: %s", exc)
