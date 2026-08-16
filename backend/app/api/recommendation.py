"""Stock recommendation API endpoints.

GET  /api/recommendation/latest   — Latest Top 10 + 3 alternates
GET  /api/recommendation/history   — Historical query by date

Rankings are produced exclusively by the server-side scheduled pipeline
(see ai_stock.pipeline.config.PIPELINE_SCHEDULE) — no manual trigger.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.dependencies import get_current_user
from app.models import User
from app.services.pipeline_service import get_pipeline_service

router = APIRouter(prefix="/recommendation", tags=["recommendation"])


@router.get("/latest", summary="最新一期寻龙榜 Top 10 + 3 备选")
def get_latest(
    current_user: User = Depends(get_current_user),
) -> dict:
    """Return the latest stock recommendations."""
    svc = get_pipeline_service()
    result = svc.get_latest()
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="暂无寻龙榜数据，系统生成中，请稍后重试",
        )
    # Return only the recommendations portion
    return {
        "snapshot": result.get("snapshot"),
        "recommendations": result.get("recommendations", []),
    }


@router.get("/history", summary="按日期查询寻龙榜结果")
def get_history(
    date: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    """Query recommendations for a specific date (YYYY-MM-DD).

    Returns empty data (not 404) when nothing exists for the date — history
    views should never trigger a pipeline run.
    """
    svc = get_pipeline_service()
    result = svc.get_by_date(date)
    if result is None:
        return {"snapshot": None, "recommendations": []}
    return {
        "snapshot": result.get("snapshot"),
        "recommendations": result.get("recommendations", []),
    }
