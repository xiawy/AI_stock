"""风控 Agent 集群 (设计文档 §7.4, 队列: risk — 异步兜底).

注意: 异步队列风控只做兜底, 交易第一道防线是同步 ``pre_trade_check``.

handlers:
- risk_scan     — 定时扫描持仓违规 → 强制调仓任务投 hold 队列; 账户指标冻结
- force_reduce  — (注册在 hold 队列) 执行强制减仓至目标占比
"""

from __future__ import annotations

import logging

from .. import db_ops
from ..broker import OrderRequest, OrderSide, OrderType, get_broker, round_lot
from ..risk_control import account_snapshot, async_risk_scan, pre_trade_check
from .base import BaseAgent

logger = logging.getLogger(__name__)


class RiskScanAgent(BaseAgent):
    """异步风控兜底扫描 (§7.4): 定时扫描持仓 + 账户全局指标监控."""

    agent_name = "risk_scan"

    def handle(self, task: dict) -> dict:
        task_id = task.get("task_id", "")
        result = async_risk_scan()
        self.log(
            "risk_scan_done",
            task_id=task_id,
            reason=f"兜底扫描完成, 发现 {len(result.get('findings', []))} 项问题",
            detail=result,
        )
        return result


def handle_force_reduce(task: dict) -> dict:
    """hold 队列 force_reduce handler: 风控强制减仓至目标占比 (§7.4).

    payload: {symbol, reason, target_pct}
    """
    payload = task.get("payload", {})
    symbol = payload.get("symbol", "")
    reason = payload.get("reason", "")
    target_pct = float(payload.get("target_pct", 0.30))
    task_id = task.get("task_id", "")

    if not symbol:
        return {"status": "skipped", "reason": "no symbol"}

    snapshot = account_snapshot()
    total_assets = snapshot.get("total_assets", 0.0) or 1.0
    broker = get_broker()
    positions = {p.symbol: p for p in broker.get_positions()}
    pos = positions.get(symbol)
    if pos is None or pos.available <= 0:
        db_ops.log_decision(
            agent="risk_force_reduce", decision="skipped", symbol=symbol,
            task_id=task_id, reason="无可卖持仓 (T+1 冻结或已清仓)",
        )
        return {"status": "skipped", "reason": "no available position"}

    target_value = total_assets * target_pct
    excess_value = pos.market_value - target_value
    quote_price = pos.last_price or 0.0
    if quote_price <= 0 or excess_value <= 0:
        return {"status": "skipped", "reason": "仓位已在目标内"}
    sell_qty = round_lot(int(excess_value / quote_price / 100) * 100)
    sell_qty = min(sell_qty, pos.available)
    if sell_qty <= 0:
        return {"status": "skipped", "reason": "减仓数量不足一手"}

    order = OrderRequest(
        symbol=symbol, side=OrderSide.SELL, quantity=sell_qty,
        order_type=OrderType.MARKET, reason=f"风控强制减仓: {reason}",
        flow_id="",
    )
    check = pre_trade_check(order, snapshot)
    if not check.ok:
        db_ops.log_decision(
            agent="risk_force_reduce", decision="risk_rejected", symbol=symbol,
            task_id=task_id, reason=f"强制减仓被风控拒绝: {check.reason}",
        )
        return {"status": "risk_rejected", "reason": check.reason}

    result = broker.place_order(order)
    db_ops.log_decision(
        agent="risk_force_reduce",
        decision="executed" if result.status.value == "filled" else "failed",
        symbol=symbol, task_id=task_id, reason=reason,
        detail={
            "quantity": result.filled_quantity,
            "price": result.avg_fill_price,
            "amount": result.amount,
        },
    )
    # 减仓后同步持仓池数量
    holding = db_ops.get_holding(symbol)
    if holding and result.status.value == "filled":
        remaining = int(holding.get("quantity", 0)) - result.filled_quantity
        if remaining <= 0:
            db_ops.delete_holding(symbol)
        else:
            db_ops.upsert_holding({
                "symbol": symbol,
                "quantity": remaining,
                "available_quantity": max(
                    0, int(holding.get("available_quantity", 0)) - result.filled_quantity,
                ),
            })
    return {
        "status": result.status.value,
        "quantity": result.filled_quantity,
        "price": result.avg_fill_price,
    }
