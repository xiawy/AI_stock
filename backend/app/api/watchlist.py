"""Watchlist endpoints (自选股管理)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies import get_current_user
from app.models import User, WatchlistItem

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


class WatchlistAdd(BaseModel):
    ticker: str = Field(min_length=1, max_length=32, description="6位代码或中文全称")
    note: str = Field(default="", max_length=255)


@router.get("", summary="我的自选股列表")
def list_watchlist(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    rows = (
        db.query(WatchlistItem)
        .filter(WatchlistItem.user_id == current_user.id)
        .order_by(WatchlistItem.created_at.desc())
        .all()
    )
    items = [row.to_dict() for row in rows]

    # Attach display names in one batch (cached per code by lru_cache).
    from app.services.stock_service import search as _search

    for item in items:
        try:
            item["label"] = _search(item["ticker"])["label"]
        except HTTPException:
            item["label"] = item["ticker"]
    return items


@router.post("", status_code=status.HTTP_201_CREATED, summary="添加自选股")
def add_watchlist(
    payload: WatchlistAdd,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    from app.services.stock_service import search as _search

    try:
        resolved = _search(payload.ticker)
    except HTTPException as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.detail
        ) from exc

    exists = (
        db.query(WatchlistItem)
        .filter(
            WatchlistItem.user_id == current_user.id,
            WatchlistItem.ticker == resolved["code"],
        )
        .first()
    )
    if exists is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="该股票已在自选列表中"
        )

    item = WatchlistItem(
        user_id=current_user.id, ticker=resolved["code"], note=payload.note
    )
    db.add(item)
    db.commit()
    out = item.to_dict()
    out["label"] = resolved["label"]
    return out


@router.delete("/{ticker}", summary="移除自选股")
def remove_watchlist(
    ticker: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    deleted = (
        db.query(WatchlistItem)
        .filter(
            WatchlistItem.user_id == current_user.id,
            WatchlistItem.ticker == ticker,
        )
        .delete()
    )
    db.commit()
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="自选列表中没有该股票"
        )
    return {"detail": "已移除"}
