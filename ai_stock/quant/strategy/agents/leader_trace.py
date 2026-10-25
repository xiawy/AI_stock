"""M2 高标梯队与溯源.

只对 ≥3 连板的标的取个股资金 —— 全市场逐个拉资金流会打爆接口限流,
且与"只关心梯队"的产品意图不符.
"""

from __future__ import annotations

from ...agents.base import BaseAgent
from ..rules import leader as leader_rules
from ..runtime import RunContext, StepResult


class LeaderTraceAgent(BaseAgent):
    agent_name = "strategy_leader_trace"

    def run(self, ctx: RunContext) -> StepResult:
        rows, ev = ctx.port.limit_up(days=5)
        ctx.add_evidence(ev)
        if not rows:
            ctx.traces = []
            return StepResult(self.agent_name, ok=False, degraded=True,
                              summary="涨停池不可用, 无法溯源高标")

        counts = leader_rules.count_boards(rows)
        leaders = [c for c, n in counts.items() if n >= leader_rules.MIN_LEADER_BOARDS]
        fund = ctx.port.fund_flow(leaders) if leaders else {}
        # 席位只查高标 (≥3 连板, 通常 <10 只): 全市场逐股查龙虎榜会限流,
        # 而限流的代价是整块数据一起缺 —— 比"少一条席位证据"严重得多.
        institutional, lhb_ev = (
            ctx.port.institutional(leaders) if leaders else ({}, None)
        )
        if lhb_ev is not None:
            ctx.add_evidence(lhb_ev)

        ctx.traces = leader_rules.build_traces(
            rows,
            official_board={},
            fund=fund,
            institutional=institutional,
        )
        ctx.ladder_health = leader_rules.ladder_health(ctx.traces)

        blocked = sum(1 for t in ctx.traces if t.exclude_from_basket)
        self.log(
            decision="computed",
            reason=f"{len(ctx.traces)} leaders, {blocked} blocked from baskets",
        )
        return StepResult(
            self.agent_name,
            ok=bool(ctx.traces),
            degraded=not ctx.traces,
            summary=f"{len(ctx.traces)} 只高标 / 梯队{ctx.ladder_health} / 剔除 {blocked}",
        )
