"""M3 影子板块资金序列 + 风格定性.

没有官方指数的结构行情, 用成分股资金加总造序列. 加总口径必须与 M1 的
basket 完全一致 (同一版成员), 否则"板块面说强、资金面说没进"这类假背离
全是口径不一致造成的.

风格定性用**全市场板块资金流**的三端均值, 而不是当日涨幅榜篮子的单边数据:
只测成长端必然得出"进攻", 那个结论没有信息量.
"""

from __future__ import annotations

from ...agents.base import BaseAgent
from ..rules import flow as flow_rules
from ..runtime import RunContext, StepResult


class FlowShadowAgent(BaseAgent):
    agent_name = "strategy_flow_shadow"

    def run(self, ctx: RunContext) -> StepResult:
        if not ctx.verdicts:
            return StepResult(self.agent_name, ok=False, degraded=True,
                              summary="无篮子, 跳过影子资金")

        shadows = []
        for v in ctx.verdicts:
            members = v.basket.members
            flows = ctx.port.fund_flow(members) if members else {}
            shadows.append(flow_rules.aggregate(v.basket.name, members, flows))

        ctx.shadows = shadows
        ctx.style_label, _dumbbell = flow_rules.style_from_boards(ctx.boards)
        usable = [s for s in shadows if s.pattern != "数据缺失"]
        if not usable:
            return StepResult(self.agent_name, ok=False, degraded=True,
                              summary="个股资金序列全部取不到")

        self.log(decision="computed",
                 reason=f"{len(usable)}/{len(shadows)} shadow series, style={ctx.style_label or '-'}")
        return StepResult(
            self.agent_name,
            summary=f"{len(usable)} 条影子序列 / 风格={ctx.style_label or '数据不足'}",
        )
