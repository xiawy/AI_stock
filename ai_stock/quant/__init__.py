"""量化交易子系统 (V3.0, 设计文档「新增量化交易」).

AI 辅助量化交易决策系统: 定时选股 → 自选池动态维护 → 买入信号二次验证 →
持仓维护与风控。多 Agent 通过 Redis (可降级 SQLite) 消息队列异步协作,
Orchestrator FSM 驱动流程, 三级缓存 + 数据源熔断, 规则引擎带强制人工
审核的自我进化流水线。

子模块:
- ``mq`` / ``mq_worker``   — Redis 消息队列 + 消费者/延迟/巡检线程 (§6)
- ``agents``               — 选股/买入/持仓/风控 Agent 集群 (§7)
- ``orchestrator``         — FSM 流程引擎 (§8)
- ``data_service`` / ``cache_manager`` / ``news_store`` — 数据服务层 (§9)
- ``rules_engine`` / ``evolution`` — 规则引擎与自我进化 (§10)
- ``risk_control``         — 同步前置风控 (§11)
- ``broker``               — SimulatedBroker + 华兴证券实盘预留 (§12)
- ``db`` / ``db_models`` / ``db_ops`` — SQLite 业务存储 (§13)
- ``ops_monitor``          — 告警分级 + 每日运行报告 (§14)
- ``scheduler``            — APScheduler 定时调度层, 回调仅入队 (§5)
- ``service``              — QuantService 一站式装配

启动: ``python -m ai_stock.quant``。本系统不构成任何投资建议, 必须优先
在模拟盘充分验证, 实盘使用风险自担。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅供静态检查; 运行时走下方惰性 __getattr__
    from .service import QuantService, get_quant_service, run_service

__all__ = [
    "QuantService",
    "get_quant_service",
    "run_service",
]


def __getattr__(name: str):
    """惰性导出: 避免 ``import ai_stock.quant`` 触发 Agent/LLM 重型依赖链."""
    if name in __all__:
        from . import service

        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
