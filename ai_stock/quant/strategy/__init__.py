"""策略台 (StrategyDesk): 收盘后的市场级研判层.

分层 (详见 docs/STRATEGY_DESK_ARCHITECTURE.md):
    contracts  纯数据契约
    rules/     纯函数判断 (禁止 import db/网络/LLM)
    ports/adapters  取数抽象 (生产 / 回放)
    agents/    取数+调用规则+挂结果 (不写库)
    repo       唯一写入口 (单事务)
    render     JSON → 三段式 (无 LLM)
    service    流程编排 / MQ handler / 回填

对外只暴露四个入口, 其余一律走内部模块, 避免调用方绕过事务边界.
"""

from __future__ import annotations

__all__ = [
    "run_strategy_flow",
    "handle_strategy_run",
    "handle_strategy_coef_update",
    "update_coefs",
    "verify_due",
    "should_run",
]


def __getattr__(name: str):
    """懒导出: 让 ``import ai_stock.quant.strategy`` 不触发 db/LLM 初始化."""
    if name in __all__:
        from . import service

        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
