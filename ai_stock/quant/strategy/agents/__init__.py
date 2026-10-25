"""策略台 Agent 集群.

形状统一: ``run(ctx) -> StepResult``. 每个 Agent 只做三件事——
取数(经 port)、调纯函数、把结果挂到 ctx. 不写库 (统一由 service 单事务落),
不互相调用 (顺序由 service.STEPS 决定).

不注册成 MQ task_type: 整个策略台是一次"流程", 只有一个队列任务
``strategy_run``, 避免六个任务类型各自重试导致半份报告.
"""

from __future__ import annotations

from .arbiter import ArbiterAgent
from .cross_check import CrossCheckAgent
from .flow_shadow import FlowShadowAgent
from .leader_trace import LeaderTraceAgent
from .macro_gate import MacroGateAgent
from .sector_structure import SectorStructureAgent

__all__ = [
    "SectorStructureAgent",
    "LeaderTraceAgent",
    "FlowShadowAgent",
    "CrossCheckAgent",
    "MacroGateAgent",
    "ArbiterAgent",
]
