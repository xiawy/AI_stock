"""Analysis endpoints: start / status / result / control / resume / tasks."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies import get_current_user
from app.models import AnalysisTask, User
from app.schemas.analysis import (
    AnalysisResultResponse,
    AnalysisStartRequest,
    TaskCreatedResponse,
    TaskStatusResponse,
)
from app.services.analysis_service import task_manager

router = APIRouter(prefix="/analysis", tags=["analysis"])


@router.post(
    "/start",
    response_model=TaskCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="启动分析任务（原 web 版「开始分析」）",
)
def start_analysis(
    payload: AnalysisStartRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    task_id = task_manager.start(
        db,
        user_id=current_user.id,
        ticker=payload.ticker,
        trade_date=payload.trade_date,
        lookback_days=payload.lookback_days,
        llm_provider=payload.llm_provider,
        quick_think_llm=payload.quick_think_llm,
        deep_think_llm=payload.deep_think_llm,
        llm_base_url=payload.llm_base_url,
        fresh=payload.fresh,
    )
    row = db.get(AnalysisTask, task_id)
    return {
        "task_id": task_id,
        "ticker": row.ticker if row else payload.ticker,
        "trade_date": payload.trade_date,
    }


@router.get(
    "/status/{task_id}",
    response_model=TaskStatusResponse,
    summary="查询分析进度（前端 2s 轮询）",
)
def get_status(
    task_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    snapshot = task_manager.snapshot(task_id)
    _sync_row(db, task_id)
    return snapshot


@router.get(
    "/result/{task_id}",
    response_model=AnalysisResultResponse,
    summary="获取分析结果（完成后可用）",
)
def get_result(
    task_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _sync_row(db, task_id)
    return task_manager.result(task_id)


@router.post("/{task_id}/pause", summary="暂停分析")
def pause_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    if not task_manager.pause(task_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="当前状态无法暂停（未在运行或已暂停）"
        )
    return {"detail": "已暂停"}


@router.post("/{task_id}/resume", summary="恢复分析")
def resume_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    if not task_manager.resume(task_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="当前状态无法恢复（未处于暂停）"
        )
    return {"detail": "已恢复"}


@router.post("/{task_id}/stop", summary="停止分析并清除断点")
def stop_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    task_manager.stop(task_id)
    return {"detail": "已停止"}


@router.get("/tasks", summary="当前用户的分析任务记录（数据库）")
def list_tasks(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = 50,
) -> list[dict]:
    rows = (
        db.query(AnalysisTask)
        .filter(AnalysisTask.user_id == current_user.id)
        .order_by(AnalysisTask.created_at.desc())
        .limit(limit)
        .all()
    )
    for row in rows:
        task_manager.sync_task_row(db, row)
    return [row.to_dict() for row in rows]


@router.get("/incomplete", summary="可从断点续跑的未完成任务")
def incomplete_tasks(
    current_user: User = Depends(get_current_user),
) -> list[dict]:
    return task_manager.incomplete_tasks()


@router.post(
    "/resume-checkpoint",
    response_model=TaskCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="从断点继续未完成任务（原 web 版「未完成任务」列表）",
)
def resume_checkpoint(
    payload: AnalysisStartRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    payload.fresh = False
    return start_analysis(payload, current_user, db)


def _sync_row(db: Session, task_id: str) -> None:
    row = db.get(AnalysisTask, task_id)
    if row is not None:
        task_manager.sync_task_row(db, row)
