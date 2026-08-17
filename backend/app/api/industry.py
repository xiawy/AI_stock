"""Industry board (行业榜) API endpoints.

GET /api/industry/latest   — Latest industry heat ranking
GET /api/industry/history  — Historical query by date

The industry board is produced by the same scheduled pipeline as the news
ranking (00:00 / 08:30 / 12:30 / 14:30); rows cascade-delete with their
snapshot after the 70-day retention window.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.dependencies import get_current_user
from app.models import User
from app.services.pipeline_service import get_pipeline_service

router = APIRouter(prefix="/industry", tags=["industry"])


@router.get("/latest", summary="最新一期行业榜（新闻热度 × 资金共振）")
def get_latest(
    current_user: User = Depends(get_current_user),
) -> dict:
    """Return the latest completed snapshot's industry rankings."""
    svc = get_pipeline_service()
    result = svc.get_industry_latest()
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="暂无行业榜数据，系统生成中，请稍后重试",
        )
    return result


@router.get("/history", summary="按日期查询行业榜")
def get_history(
    date: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    """Query industry rankings for a specific date (YYYY-MM-DD).

    Returns empty data (not 404) when nothing exists for the date.
    """
    svc = get_pipeline_service()
    result = svc.get_industry_by_date(date)
    if result is None:
        return {"snapshot": None, "rankings": []}
    return result


@router.get("/{ranking_id}/news", summary="查看行业对应新闻")
def get_industry_news(
    ranking_id: int,
    current_user: User = Depends(get_current_user),
) -> dict:
    """News items that fed one industry-ranking row's heat score.

    Same snapshot, filtered by the row's industry in each news item's
    industries list, ordered by composite score.
    """
    svc = get_pipeline_service()
    result = svc.get_industry_news(ranking_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该行业榜单记录",
        )
    return result
