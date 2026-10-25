"""策略台 API.

设计约束 (§7.5): 所有端点**只读**策略台自己的表, 不触碰订单/持仓.
写操作只有两个: 手动跑一次 (入队) 与回填到期断言 —— 都是幂等的.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from app.dependencies import get_current_user
from app.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/strategy", tags=["strategy"])

_READY = False


def _ensure_ready() -> None:
    """幂等建表: 策略台表全部靠 create_all, 上线动作只有重启."""
    global _READY
    if _READY:
        return
    from app.services.quant_service import prepare_quant_env

    prepare_quant_env()
    from ai_stock.quant import db

    db.init_quant_db()
    _READY = True


@router.get("/desk")
def get_desk(date: Optional[str] = None, snapshot_type: str = "close",
             _: User = Depends(get_current_user)):
    """三段式报告 (大盘总览 / 关注板块 / 交易建议)."""
    _ensure_ready()
    from ai_stock.quant.calendar_utils import get_trade_date
    from ai_stock.quant.strategy.repo import StrategyRepo

    trade_date = date or str(get_trade_date())
    report = StrategyRepo().load_report(trade_date, snapshot_type)
    if report is None:
        return {"trade_date": trade_date, "empty": True,
                "message": "该日尚无策略台报告"}
    return report


@router.get("/boards")
def get_boards(date: Optional[str] = None, _: User = Depends(get_current_user)):
    """板块结构拆解明细 (含 basket 与证据分布)."""
    _ensure_ready()
    from ai_stock.quant.calendar_utils import get_trade_date
    from ai_stock.quant.strategy.repo import StrategyRepo

    trade_date = date or str(get_trade_date())
    report = StrategyRepo().load_report(trade_date)
    if report is None:
        return {"trade_date": trade_date, "items": []}
    return {"trade_date": trade_date,
            "items": report["sections"].get("boards", [])}


@router.get("/leaders")
def get_leaders(date: Optional[str] = None, _: User = Depends(get_current_user)):
    """高标梯队与溯源 (不可外推的标的会带 warn 字段)."""
    _ensure_ready()
    from ai_stock.quant.calendar_utils import get_trade_date
    from ai_stock.quant.strategy.repo import StrategyRepo

    trade_date = date or str(get_trade_date())
    report = StrategyRepo().load_report(trade_date)
    if report is None:
        return {"trade_date": trade_date, "items": []}
    return {"trade_date": trade_date,
            "items": report["sections"].get("leaders", [])}


@router.get("/scoreboard")
def get_scoreboard(_: User = Depends(get_current_user)):
    """记分看板: 分母只算 judged, unjudged 单列 (数据故障 ≠ 判断错误)."""
    _ensure_ready()
    from ai_stock.quant.strategy.repo import StrategyRepo

    return StrategyRepo().score()


@router.get("/coef")
def get_coef(_: User = Depends(get_current_user)):
    """权重回灌现状: 系数、未出系数的原因、开关状态.

    "为什么本周没调权"必须能回答, 否则用户只会看到一行没变的分数.
    """
    _ensure_ready()
    from ai_stock.quant.strategy import feedback

    payload = feedback.read_payload()
    return {**payload, "enabled": feedback.enabled()}


@router.post("/coef/recompute")
def recompute_coef(_: User = Depends(get_current_user)):
    """立即重算系数 (幂等). 与周一 06:30 的定时任务走同一个函数."""
    _ensure_ready()
    from ai_stock.quant.strategy.service import update_coefs

    return update_coefs()


@router.post("/run")
def run_now(date: Optional[str] = None, snapshot_type: str = "close",
            _: User = Depends(get_current_user)):
    """手动触发 (入队, 不阻塞请求)."""
    _ensure_ready()
    from ai_stock.quant.scheduler import trigger_strategy_desk

    task_id = trigger_strategy_desk(snapshot_type=snapshot_type)
    if not task_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="今日该快照已入队或非交易日")
    return {"task_id": task_id, "snapshot_type": snapshot_type}


@router.post("/verify")
def verify(asof: Optional[str] = None, _: User = Depends(get_current_user)):
    """回填到期断言 (幂等). 只用已收盘日线, 取不到记 unjudged."""
    _ensure_ready()
    from ai_stock.quant.strategy.service import verify_due

    return verify_due(asof)
