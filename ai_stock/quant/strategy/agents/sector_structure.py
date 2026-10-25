"""M1 板块结构拆解.

回答"涨的是哪一拨". 数据全部来自现有 DataService, 无新增依赖.
"""

from __future__ import annotations

from ...config import (
    STRATEGY_CONSTITUENTS,
    STRATEGY_MAX_BASKETS,
    STRATEGY_MIN_CLUSTER,
    STRATEGY_TOP_BOARDS,
)
from ...agents.base import BaseAgent
from ..rules import basket as basket_rules
from ..runtime import RunContext, StepResult


class SectorStructureAgent(BaseAgent):
    agent_name = "strategy_sector_structure"

    def run(self, ctx: RunContext) -> StepResult:
        boards, ev = ctx.port.board_flow()
        ctx.add_evidence(ev)
        if not boards:
            return StepResult(self.agent_name, ok=False, degraded=True,
                              summary="板块资金流不可用")

        top = sorted(boards, key=lambda b: b.change_pct, reverse=True)[:STRATEGY_TOP_BOARDS]
        verdicts = []
        tags_by_code = self._reason_tags(ctx)

        for board in top:
            members = ctx.port.constituents(board.code, top_n=STRATEGY_CONSTITUENTS)
            if not members:
                continue
            concepts = {m.code: ctx.port.concepts(m.code) for m in members}
            drafts = basket_rules.build_drafts(
                board, members, concepts, tags_by_code,
                min_cluster=STRATEGY_MIN_CLUSTER,
                max_baskets=STRATEGY_MAX_BASKETS,
            )
            verdicts.extend(basket_rules.synthesize(d) for d in drafts)

        ctx.verdicts = verdicts
        ctx.boards = boards
        if not verdicts:
            return StepResult(self.agent_name, ok=False, degraded=True,
                              summary=f"{len(top)} 个板块均拆不出子篮子")

        board_level = sum(1 for v in verdicts if v.verdict == "板块级")
        self.log(
            decision="computed",
            reason=f"{len(verdicts)} baskets, {board_level} board-level",
        )
        return StepResult(
            self.agent_name, summary=f"{len(verdicts)} 篮子 / {board_level} 可作板块",
        )

    def _reason_tags(self, ctx: RunContext) -> dict[str, list[str]]:
        """涨停池归因标签: 唯一的 C 级证据来源 (编辑归因, 未取一手)."""
        rows, ev = ctx.port.limit_up(days=2)
        ctx.add_evidence(ev)
        tags: dict[str, list[str]] = {}
        for row in rows:
            tags.setdefault(row.code, []).extend(row.reason_tags)
        return tags
