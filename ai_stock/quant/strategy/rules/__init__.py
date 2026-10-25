"""纯函数规则层.

本包**禁止** import: 数据库 / 网络 / LLM (见 docs/STRATEGY_DESK_ARCHITECTURE.md §1.2).
这条依赖线一破, 20 个交易日的回放能力立刻消失.
"""

from . import basket, claim, flow, leader, matrix, news, regime, textmatch

__all__ = [
    "basket", "claim", "flow", "leader", "matrix", "news", "regime", "textmatch",
]
