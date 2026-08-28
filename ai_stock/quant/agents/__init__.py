"""Quant agent clusters (设计文档 §7) — 队列 handler 注册中心.

每个 Agent 都是 Redis/SQLite 队列消费者 handler; ``build_handlers`` 按
队列返回 {task_type: handler} 注册表, 供 QuantService 创建 TaskConsumer.

队列 → 集群:
- selection_control  → 选股集群 (§7.1) + selection_start 入口
- buy                → 买入集群 (§7.2) + buy_scan 入口
- hold               → 持仓维护集群 (§7.3) + hold_scan 入口 + force_reduce
- risk               → 风控集群 (§7.4, 异步兜底)
- orchestrator       → FSM 流程引擎事件 (§8)
"""

from __future__ import annotations

from typing import Callable

from .base import BaseAgent
from .buy import (
    CatalystAgent,
    LogicCollapseAgent,
    PositionOpenAgent,
    SecondVerificationAgent,
    TechSignalAgent,
    handle_buy_scan,
)
from .hold import (
    AddPositionAgent,
    ExecutionUpdateAgent,
    ReducePositionAgent,
    TrendTrackingAgent,
    handle_hold_scan,
)
from .risk import RiskScanAgent, handle_force_reduce
from .selection import (
    DeepAnalysisAgent,
    IndustryScanAgent,
    LimitUpMonitorAgent,
    StockSelectionAgent,
    handle_selection_start,
)

__all__ = [
    "BaseAgent",
    "IndustryScanAgent",
    "LimitUpMonitorAgent",
    "StockSelectionAgent",
    "DeepAnalysisAgent",
    "LogicCollapseAgent",
    "TechSignalAgent",
    "CatalystAgent",
    "SecondVerificationAgent",
    "PositionOpenAgent",
    "TrendTrackingAgent",
    "AddPositionAgent",
    "ReducePositionAgent",
    "ExecutionUpdateAgent",
    "RiskScanAgent",
    "build_handlers",
]


def build_handlers() -> dict[str, dict[str, Callable[[dict], dict]]]:
    """构建 queue_name → {task_type: handler} 注册表 (每次调用新建实例)."""
    from ..config import (
        BUY_QUEUE,
        HOLD_QUEUE,
        ORCHESTRATOR_QUEUE,
        RISK_QUEUE,
        SELECTION_QUEUE,
    )
    from ..orchestrator import get_orchestrator

    selection_agents = {
        "industry_scan": IndustryScanAgent().handle,
        "limit_up_monitor": LimitUpMonitorAgent().handle,
        "stock_selection": StockSelectionAgent().handle,
        "deep_analysis": DeepAnalysisAgent().handle,
    }
    buy_agents = {
        "logic_collapse_check": LogicCollapseAgent().handle,
        "tech_signal": TechSignalAgent().handle,
        "catalyst_event": CatalystAgent().handle,
        "second_verification": SecondVerificationAgent().handle,
        "position_open": PositionOpenAgent().handle,
    }
    hold_agents = {
        "trend_tracking": TrendTrackingAgent().handle,
        "add_position_decision": AddPositionAgent().handle,
        "reduce_position_decision": ReducePositionAgent().handle,
        "execution_update": ExecutionUpdateAgent().handle,
    }

    return {
        SELECTION_QUEUE: {
            "selection_start": handle_selection_start,
            **selection_agents,
        },
        BUY_QUEUE: {
            "buy_scan": handle_buy_scan,
            **buy_agents,
        },
        HOLD_QUEUE: {
            "hold_scan": handle_hold_scan,
            "force_reduce": handle_force_reduce,
            **hold_agents,
        },
        RISK_QUEUE: {
            "risk_scan": RiskScanAgent().handle,
        },
        ORCHESTRATOR_QUEUE: get_orchestrator().get_handlers(),
    }
