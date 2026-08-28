"""Evolution review API — human-in-the-loop strategy review.

Endpoints (all require login):
    GET    /api/evolution/agents                      — per-agent status overview
    POST   /api/evolution/review/{agent}              — trigger a review (background job)
    GET    /api/evolution/review/jobs/{job_id}        — poll a review job
    GET    /api/evolution/drafts                      — list pending drafts
    GET    /api/evolution/drafts/{agent}/{filename}   — view draft content
    POST   /api/evolution/drafts/{agent}/{filename}/approve  — approve & apply
    DELETE /api/evolution/drafts/{agent}/{filename}   — reject & delete
    GET    /api/evolution/learnings/{agent}           — latest learning summary

Safety: reviews only ever produce drafts (and learning summaries). Nothing in
this module modifies the active ``custom_strategies/`` tree except
``approve_draft``, which requires an explicit human action.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.dependencies import get_current_user
from app.models import User
from app.services.evolution_service import get_evolution_service

router = APIRouter(prefix="/evolution", tags=["evolution"])


@router.get("/agents", summary="各 Agent 进化状态概览")
def list_agents(current_user: User = Depends(get_current_user)) -> dict:
    from ai_stock.default_config import DEFAULT_CONFIG
    from ai_stock.evolution import review_service

    return {"agents": review_service.agent_status(DEFAULT_CONFIG)}


@router.post(
    "/review/{agent}",
    status_code=status.HTTP_202_ACCEPTED,
    summary="触发一次复盘（后台执行）",
)
def trigger_review(agent: str, current_user: User = Depends(get_current_user)) -> dict:
    svc = get_evolution_service()
    try:
        job_id = svc.start_review(agent)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return {"job_id": job_id, "agent": agent, "status": "running"}


@router.get("/review/jobs/{job_id}", summary="查询复盘任务进度")
def job_status(job_id: str, current_user: User = Depends(get_current_user)) -> dict:
    return get_evolution_service().get_job(job_id)


@router.get("/drafts", summary="待审核策略草稿列表")
def list_drafts(
    agent: str | None = None,
    current_user: User = Depends(get_current_user),
) -> dict:
    drafts = get_evolution_service().list_drafts(agent_name=agent)
    return {"drafts": drafts}


@router.get("/drafts/{agent}/{filename}", summary="查看草稿内容")
def draft_content(
    agent: str,
    filename: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    return get_evolution_service().get_draft(agent, filename)


@router.post(
    "/drafts/{agent}/{filename}/approve",
    summary="批准并应用草稿（自动备份原策略）",
)
def approve_draft(
    agent: str,
    filename: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    result = get_evolution_service().approve_draft(agent, filename)
    return {"detail": "已批准并应用", **result}


@router.delete("/drafts/{agent}/{filename}", summary="拒绝并删除草稿")
def reject_draft(
    agent: str,
    filename: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    result = get_evolution_service().reject_draft(agent, filename)
    return {"detail": "已拒绝并删除", **result}


@router.get("/learnings/{agent}", summary="最新复盘总结")
def latest_learning(
    agent: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    from ai_stock.default_config import DEFAULT_CONFIG
    from ai_stock.evolution import review_service

    learning = review_service.latest_learning(DEFAULT_CONFIG, agent)
    return {"learning": learning}
