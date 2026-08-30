"""Hot stock (热股榜) API endpoints.

GET  /api/recommendation/latest   — 自选池活跃标的 (quant 选股流程产出)
GET  /api/recommendation/history  — Historical query by date (自选池当日快照)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.dependencies import get_current_user
from app.models import User
from app.services.pipeline_service import get_pipeline_service

router = APIRouter(prefix="/recommendation", tags=["recommendation"])


@router.get("/latest", summary="最新一期热股榜（自选池活跃标的）")
def get_latest(
    current_user: User = Depends(get_current_user),
) -> dict:
    """Return the active optional-pool stocks as the hot-stock list."""
    svc = get_pipeline_service()
    result = svc.get_hot_stocks_latest()
    if not result.get("recommendations"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="暂无热股榜数据，选股流程运行后自动更新，请稍后重试",
        )
    return result


@router.get("/history", summary="按日期查询热股榜结果")
def get_history(
    date: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    """Query the optional-pool snapshot for a specific date (YYYY-MM-DD).

    Returns empty data (not 404) when nothing exists for the date — history
    views should never trigger a pipeline run.
    """
    svc = get_pipeline_service()
    return svc.get_hot_stocks_by_date(date)
