"""Industry board (行业榜) API endpoints.

GET /api/industry/latest   — Latest industry board
GET /api/industry/history  — Historical query by date

The industry board is produced by the quant selection flow (生命周期排序
前 10 行业, 每行业含龙头股), stored in quant_industry_board; rows older
than the 70-day retention window are cleaned daily.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.dependencies import get_current_user
from app.models import User
from app.services.pipeline_service import get_pipeline_service

router = APIRouter(prefix="/industry", tags=["industry"])


@router.get("/latest", summary="最新一期行业榜（quant 选股生命周期排序）")
def get_latest(
    current_user: User = Depends(get_current_user),
) -> dict:
    """Return the latest industry board produced by the quant selection flow."""
    svc = get_pipeline_service()
    result = svc.get_industry_latest()
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="暂无行业榜数据，选股流程运行后自动更新，请稍后重试",
        )
    return result


@router.get("/history", summary="按日期查询行业榜")
def get_history(
    date: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    """Query the industry board for a specific date (YYYY-MM-DD).

    Returns empty data (not 404) when nothing exists for the date.
    """
    svc = get_pipeline_service()
    result = svc.get_industry_by_date(date)
    if result is None:
        return {"rank_date": date, "rankings": []}
    return result


@router.get("/{board_id}/news", summary="查看行业对应新闻")
def get_industry_news(
    board_id: int,
    current_user: User = Depends(get_current_user),
) -> dict:
    """News items related to one industry-board row (按行业名匹配最新新闻快照)."""
    svc = get_pipeline_service()
    result = svc.get_industry_news(board_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到该行业榜单记录",
        )
    return result
