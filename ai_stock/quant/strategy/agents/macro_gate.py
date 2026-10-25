"""M4 宏观闸门.

只产出约束, 不产出方向. 缺数据时 growth_cap=None —— 语义是"不约束".
"""

from __future__ import annotations

from ...agents.base import BaseAgent
from ..rules import regime as regime_rules
from ..runtime import RunContext, StepResult


class MacroGateAgent(BaseAgent):
    agent_name = "strategy_macro_gate"

    def run(self, ctx: RunContext) -> StepResult:
        vars_, ev = ctx.port.macro()
        ctx.add_evidence(ev)

        ctx.regime = regime_rules.decide(vars_)
        self.log(
            decision=ctx.regime.code,
            reason=ctx.regime.note[:180],
        )
        return StepResult(
            self.agent_name,
            ok=ctx.regime.code != "未定",
            degraded=ctx.regime.code == "未定",
            summary=f"regime={ctx.regime.code} 上限={ctx.regime.growth_cap or '不约束'}",
        )
