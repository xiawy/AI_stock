"""Stock endpoints: search / quote / kline."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.dependencies import get_current_user
from app.models import User
from app.schemas.stocks import KlineResponse, QuoteResponse, StockSearchResponse
from app.services import stock_service

router = APIRouter(prefix="/stocks", tags=["stocks"])


@router.get(
    "/search",
    response_model=StockSearchResponse,
    summary="搜索股票（代码或中文名称 → 6位代码）",
)
def search(
    q: str = Query(min_length=1, max_length=32, description="6位代码 / SH600519 / 中文名称"),
    current_user: User = Depends(get_current_user),
) -> dict:
    return stock_service.search(q)


@router.get(
    "/{code}/quote",
    response_model=QuoteResponse,
    summary="个股实时行情（腾讯源，与管线同源）",
)
def quote(code: str, current_user: User = Depends(get_current_user)) -> dict:
    return stock_service.quote(code)


@router.get(
    "/{code}/kline",
    response_model=KlineResponse,
    summary="个股日K线数据（ECharts 蜡烛图）",
)
def kline(
    code: str,
    days: int = Query(default=120, ge=10, le=800),
    current_user: User = Depends(get_current_user),
) -> dict:
    return stock_service.kline(code, days)
