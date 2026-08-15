"""Impact assessment API endpoints.

GET /api/impact/latest      — Latest Top 20 impact ranking
GET /api/impact/history      — Historical query by date
GET /api/impact/detail/{id}  — Single news item detail
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.dependencies import get_current_user
from app.models import User
from app.services.pipeline_service import get_pipeline_service

router = APIRouter(prefix="/impact", tags=["impact"])


@router.get("/latest", summary="最新一期 Top 20 影响力榜")
def get_latest(
    current_user: User = Depends(get_current_user),
) -> dict:
    """Return the latest completed pipeline run's Top 20 + recommendations."""
    svc = get_pipeline_service()
    result = svc.get_latest()
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="暂无影响力评估数据，请先手动触发流水线",
        )
    return result


@router.get("/history", summary="按日期查询影响力榜")
def get_history(
    date: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    """Query impact ranking for a specific date (YYYY-MM-DD)."""
    svc = get_pipeline_service()
    result = svc.get_by_date(date)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到 {date} 的影响力评估数据",
        )
    return result


@router.get("/detail/{news_id}", summary="单条新闻评估详情")
def get_detail(
    news_id: int,
    current_user: User = Depends(get_current_user),
) -> dict:
    """Get detailed evaluation for a single news item."""
    try:
        from ai_stock.pipeline.db_ops import _get_session
        from ai_stock.pipeline.db_models import NewsItem

        session = _get_session()
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="数据库不可用",
            )

        try:
            item = session.get(NewsItem, news_id)
            if item is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"未找到新闻 ID {news_id}",
                )
            return item.to_dict()
        finally:
            session.close()

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )
