"""Quant trading admin API (设计文档 §4 FastAPI 可选管理接口).

All endpoints are authenticated (same as the rest of the API) and talk to the
quant engine (``ai_stock.quant``) through its ``db_ops`` / ``rules_engine`` /
``scheduler`` facades — no engine internals are bypassed.

Read endpoints work even when the quant service (consumers + scheduler) is
not running (e.g. tests with ``AISTOCK_DISABLE_SCHEDULERS=1``): the quant DB
is initialized lazily on first access. Trigger endpoints enqueue tasks; they
are consumed once the service is up.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.dependencies import get_current_user
from app.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/quant", tags=["quant"])

# ---------------------------------------------------------------------------
# Lazy engine bootstrap (tables + seed rules/config, idempotent)
# ---------------------------------------------------------------------------

_READY = False


def _ensure_ready() -> None:
    """Idempotent: pin env, create quant tables, seed config/rules."""
    global _READY
    if _READY:
        return
    from app.services.quant_service import prepare_quant_env

    prepare_quant_env()
    from ai_stock.quant import db, rules_engine
    from ai_stock.quant.service import _seed_system_config

    db.init_quant_db()
    _seed_system_config()
    rules_engine.seed_rules_and_cases()
    _READY = True


def _db_ops():
    _ensure_ready()
    from ai_stock.quant import db_ops

    return db_ops


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class ConfigUpdate(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    value: str = Field(max_length=2000)
    description: str = ""


class PoolStatusUpdate(BaseModel):
    # active=可买入 / hold=暂缓 / removed=移除 / stop_holding=停止持仓维护
    status: str = Field(pattern=r"^(active|hold|removed|stop_holding)$")


class RuleToggle(BaseModel):
    enabled: bool


class ReviewNote(BaseModel):
    note: str = ""


class EvolveRequest(BaseModel):
    symbols: Optional[list[str]] = None


class MeBuyRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    quantity: int = Field(default=0, ge=0, description="缺省=可用资金×20% 整手")
    price: Optional[float] = Field(default=None, gt=0, description="缺省=实时行情(含滑点)")
    reason: str = Field(default="手动买入", max_length=500)


class MeSellRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    portion: float = Field(default=1.0, gt=0, le=1.0, description="卖出比例, 1=清仓")
    quantity: int = Field(default=0, ge=0, description="指定数量优先于比例")
    price: Optional[float] = Field(default=None, gt=0, description="缺省=实时行情(扣滑点)")
    reason: str = Field(default="手动卖出", max_length=500)


# ---------------------------------------------------------------------------
# 状态 / 配置
# ---------------------------------------------------------------------------


@router.get("/status", summary="量化服务状态总览")
def quant_status(current_user: User = Depends(get_current_user)) -> dict:
    from app.services.quant_service import get_quant_status

    ops = _db_ops()
    info = get_quant_status()
    info["config"] = {
        key: ops.get_config_value(key)
        for key in (
            "global_trade_enable",
            "max_positions",
            "position_ratio_pct",
            "stop_loss_pct",
            "time_stop_days",
            "trade_frozen",
        )
    }
    info["optional_pool_active"] = len(ops.get_optional_pool("active"))
    info["holdings"] = len(ops.get_holdings("__all__"))
    info["user_accounts"] = len(ops.get_user_accounts())
    info["pending_tasks"] = ops.count_tasks("pending")
    info["dead_letter_tasks"] = ops.count_tasks("dead")
    return info


@router.get("/config", summary="系统配置全量")
def get_config(current_user: User = Depends(get_current_user)) -> dict:
    return {"config": _db_ops().get_all_config()}


@router.put("/config", summary="更新单项配置 (如 global_trade_enable)")
def update_config(
    body: ConfigUpdate,
    current_user: User = Depends(get_current_user),
) -> dict:
    ops = _db_ops()
    ops.set_config_value(body.key, body.value, body.description)
    ops.log_decision(
        agent="api",
        decision="config_update",
        reason=f"{body.key} -> {body.value}",
    )
    return {"key": body.key, "value": ops.get_config_value(body.key)}


# ---------------------------------------------------------------------------
# 自选池 (§2.4)
# ---------------------------------------------------------------------------


@router.get("/pool", summary="自选池列表")
def list_pool(
    status_filter: Optional[str] = None,
    current_user: User = Depends(get_current_user),
) -> dict:
    ops = _db_ops()
    if status_filter:
        items = ops.get_optional_pool(status_filter)
    else:
        items = [
            item
            for st in ("active", "hold", "removed", "stop_holding")
            for item in ops.get_optional_pool(st)
        ]
    return {"pool": items}


@router.get("/pool/{symbol}", summary="自选池单标的")
def get_pool_item(
    symbol: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    item = _db_ops().get_optional_stock(symbol)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{symbol} 不在自选池")
    return item


@router.put("/pool/{symbol}/status", summary="人工调整自选池状态")
def set_pool_status(
    symbol: str,
    body: PoolStatusUpdate,
    current_user: User = Depends(get_current_user),
) -> dict:
    ops = _db_ops()
    if not ops.update_optional_status(symbol, body.status, remove_reason="manual_api"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{symbol} 不在自选池")
    ops.log_decision(
        agent="api",
        decision=f"pool_{body.status}",
        symbol=symbol,
        reason="manual_api",
    )
    return {"symbol": symbol, "status": body.status}


# ---------------------------------------------------------------------------
# 持仓 / 交易
# ---------------------------------------------------------------------------


@router.get("/holdings", summary="持仓池 (管理视图: 全部用户; user_id 可过滤)")
def list_holdings(
    user_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
) -> dict:
    return {"holdings": _db_ops().get_holdings(user_id or "__all__")}


@router.get("/trades", summary="交易流水 (管理视图: 全部用户; 按日期/标的/用户过滤)")
def list_trades(
    date: Optional[str] = None,
    symbol: Optional[str] = None,
    user_id: Optional[str] = None,
    limit: int = 200,
    current_user: User = Depends(get_current_user),
) -> dict:
    ops = _db_ops()
    trades = ops.get_trades(
        date_str=date, symbol=symbol, limit=min(limit, 1000),
        user_id=user_id or "__all__",
    )
    return {
        "trades": trades,
        "realized_pnl": ops.get_realized_pnl_today(date, user_id=user_id or "__all__"),
    }


# ---------------------------------------------------------------------------
# 用户模拟账户 (/quant/me) — 引擎决策扇出 + 手动买卖, 每用户独立资金/盈亏统计
# ---------------------------------------------------------------------------


def _user_accounts_module():
    _ensure_ready()
    from ai_stock.quant import user_accounts

    return user_accounts


def _my_account(current_user: User) -> dict:
    ua = _user_accounts_module()
    account = ua.get_or_create_account(str(current_user.id), current_user.username)
    if account is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "模拟账户开通失败")
    return account


@router.get("/me/account", summary="我的模拟账户快照 (资金/持仓市值/总收益)")
def my_account(current_user: User = Depends(get_current_user)) -> dict:
    _my_account(current_user)  # 幂等开户 (首次访问按配置起始资金)
    return _user_accounts_module().user_portfolio_snapshot(str(current_user.id))


@router.get("/me/holdings", summary="我的持仓")
def my_holdings(current_user: User = Depends(get_current_user)) -> dict:
    _my_account(current_user)
    return {"holdings": _db_ops().get_holdings(str(current_user.id))}


@router.get("/me/trades", summary="我的交易流水")
def my_trades(
    date: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = 200,
    current_user: User = Depends(get_current_user),
) -> dict:
    uid = str(current_user.id)
    ops = _db_ops()
    return {
        "trades": ops.get_trades(
            date_str=date, symbol=symbol, limit=min(limit, 1000), user_id=uid,
        ),
        "realized_pnl": ops.get_realized_pnl_today(date, user_id=uid),
    }


@router.get("/me/stats", summary="我的交易统计 (胜率/累计盈亏)")
def my_stats(current_user: User = Depends(get_current_user)) -> dict:
    uid = str(current_user.id)
    _my_account(current_user)
    return _db_ops().get_user_trade_stats(uid)


@router.post("/me/buy", summary="手动买入 (模拟盘)")
def my_buy(
    body: MeBuyRequest,
    current_user: User = Depends(get_current_user),
) -> dict:
    _my_account(current_user)
    result = _user_accounts_module().execute_user_buy(
        str(current_user.id),
        body.symbol,
        price=body.price or 0.0,
        quantity=body.quantity,
        reason=body.reason,
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("reason", "买入失败"))
    return result


@router.post("/me/sell", summary="手动卖出 (模拟盘, 自动记录该笔盈亏)")
def my_sell(
    body: MeSellRequest,
    current_user: User = Depends(get_current_user),
) -> dict:
    _my_account(current_user)
    result = _user_accounts_module().execute_user_sell(
        str(current_user.id),
        body.symbol,
        portion=body.portion,
        quantity=body.quantity,
        price=body.price or 0.0,
        reason=body.reason,
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("reason", "卖出失败"))
    return result


@router.get("/accounts", summary="全部用户模拟账户 (管理视图)")
def list_accounts(current_user: User = Depends(get_current_user)) -> dict:
    ops = _db_ops()
    accounts = []
    for acct in ops.get_user_accounts():
        stats = ops.get_user_trade_stats(acct["user_id"])
        accounts.append({
            **acct,
            "holdings": len(ops.get_holdings(acct["user_id"])),
            "closed_trades": stats["closed_trades"],
            "win_rate": stats["win_rate"],
            "total_realized_pnl": stats["total_realized_pnl"],
        })
    return {"accounts": accounts}


@router.get("/decisions", summary="Agent 决策日志 (审计)")
def list_decisions(
    agent: Optional[str] = None,
    symbol: Optional[str] = None,
    flow_id: Optional[str] = None,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
) -> dict:
    decisions = _db_ops().get_decisions(
        limit=min(limit, 500), agent=agent, symbol=symbol, flow_id=flow_id,
    )
    return {"decisions": decisions}


# ---------------------------------------------------------------------------
# 规则引擎与进化审核 (§10)
# ---------------------------------------------------------------------------


@router.get("/rules", summary="策略规则列表")
def list_rules(
    enabled_only: bool = False,
    current_user: User = Depends(get_current_user),
) -> dict:
    return {"rules": _db_ops().get_rules(enabled_only=enabled_only)}


@router.get("/rules/{rule_id}/cases", summary="规则单测用例")
def list_rule_cases(
    rule_id: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    return {"cases": _db_ops().get_test_cases(rule_id)}


@router.post("/rules/{rule_id}/test", summary="执行规则单测")
def test_rule(
    rule_id: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    from ai_stock.quant import rules_engine

    _ensure_ready()
    passed, results = rules_engine.run_rule_test_cases(rule_id)
    return {"passed": passed, "results": results}


@router.put("/rules/{rule_id}", summary="启用/停用既有规则")
def toggle_rule(
    rule_id: str,
    body: RuleToggle,
    current_user: User = Depends(get_current_user),
) -> dict:
    ops = _db_ops()
    rule = next((r for r in ops.get_rules() if r["rule_id"] == rule_id), None)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"规则 {rule_id} 不存在")
    ops.upsert_rule({**rule, "enabled": body.enabled})
    ops.log_decision(
        agent="api",
        decision="rule_toggle",
        reason=f"{rule_id} enabled={body.enabled}",
    )
    return {"rule_id": rule_id, "enabled": body.enabled}


@router.post("/rules/{rule_id}/approve", summary="人工审核通过候选规则 (灰度)")
def approve_rule(
    rule_id: str,
    body: ReviewNote,
    current_user: User = Depends(get_current_user),
) -> dict:
    from ai_stock.quant import rules_engine

    _ensure_ready()
    if not rules_engine.approve_rule(rule_id, body.note):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"无可审核记录: 规则不存在或没有 pending_review 的进化记录",
        )
    return {"rule_id": rule_id, "status": "approved"}


@router.post("/rules/{rule_id}/reject", summary="驳回候选规则")
def reject_rule(
    rule_id: str,
    body: ReviewNote,
    current_user: User = Depends(get_current_user),
) -> dict:
    from ai_stock.quant import rules_engine

    _ensure_ready()
    if not rules_engine.reject_rule(rule_id, body.note):
        raise HTTPException(status.HTTP_409_CONFLICT, "无可审核记录")
    return {"rule_id": rule_id, "status": "rejected"}


@router.get("/evolutions", summary="进化记录列表")
def list_evolutions(
    status_filter: Optional[str] = None,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
) -> dict:
    records = _db_ops().get_evolution_records(status=status_filter, limit=min(limit, 200))
    return {"evolutions": records}


@router.post("/evolutions/{evo_id}/approve", summary="审核通过进化记录 (联动规则灰度)")
def approve_evolution(
    evo_id: int,
    body: ReviewNote,
    current_user: User = Depends(get_current_user),
) -> dict:
    from ai_stock.quant import rules_engine

    ops = _db_ops()
    record = next(
        (r for r in ops.get_evolution_records(limit=200) if r["id"] == evo_id),
        None,
    )
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"进化记录 {evo_id} 不存在")
    if record.get("status") != "pending_review":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"记录状态为 {record.get('status')}, 仅 pending_review 可审核",
        )
    if not rules_engine.approve_rule(record["rule_id"], body.note):
        raise HTTPException(status.HTTP_409_CONFLICT, "规则审核失败 (规则缺失)")
    return {"evolution_id": evo_id, "status": "approved", "rule_id": record["rule_id"]}


@router.post("/evolutions/{evo_id}/reject", summary="驳回进化记录")
def reject_evolution(
    evo_id: int,
    body: ReviewNote,
    current_user: User = Depends(get_current_user),
) -> dict:
    ops = _db_ops()
    if not ops.update_evolution_status(evo_id, "rejected", body.note or "manual_api"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"进化记录 {evo_id} 不存在")
    return {"evolution_id": evo_id, "status": "rejected"}


@router.post("/evolve", summary="触发止损参数进化 (后台运行)")
def trigger_evolve(
    body: EvolveRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
) -> dict:
    from ai_stock.quant import evolution

    _ensure_ready()

    def _run() -> None:
        try:
            result = evolution.evolve_stop_loss(symbols=body.symbols)
            logger.info("Evolution finished: %s", result.get("status"))
        except Exception as exc:
            logger.exception("Evolution failed: %s", exc)

    background_tasks.add_task(_run)
    return {"status": "started", "note": "进化在后台运行, 结果见 /api/quant/evolutions"}


# ---------------------------------------------------------------------------
# 日报与手动触发 (§14 / §5)
# ---------------------------------------------------------------------------


@router.get("/daily-report", summary="每日运行报告")
def daily_report(
    date: Optional[str] = None,
    current_user: User = Depends(get_current_user),
) -> dict:
    from ai_stock.quant import ops_monitor

    _ensure_ready()
    report = ops_monitor.generate_daily_report(date)
    markdown = ""
    try:
        from pathlib import Path

        path = Path(report.get("report_file", ""))
        if path.is_file():
            markdown = path.read_text(encoding="utf-8")
    except Exception:
        pass
    return {"report": report, "markdown": markdown}


_TRIGGER_ACTIONS = ("selection", "buy_scan", "hold_scan", "risk_scan")


@router.post("/trigger/{action}", summary="手动触发调度任务 (强制入队)")
def trigger_action(
    action: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    if action not in _TRIGGER_ACTIONS:
        raise HTTPException(
            422,
            f"action 必须是 {list(_TRIGGER_ACTIONS)} 之一",
        )
    from ai_stock.quant import scheduler

    _ensure_ready()
    fn = getattr(scheduler, f"trigger_{action}")
    task_id = fn(force=True)
    ops = _db_ops()
    ops.log_decision(agent="api", decision=f"trigger_{action}", task_id=task_id or "")
    return {"action": action, "enqueued": task_id is not None, "task_id": task_id}
