"""M5 交叉验证矩阵.

三条证据链 (板块结构 / 资金 / 新闻) 各自独立取数, 再按**篮子**对齐评方向.
纯规则, 不用 LLM: 同向判定是符号运算, 交给模型只会让同一输入得出不同结论,
而它恰恰是"共振 / 背离 / 伪共振"的唯一证据.

这一层单独成步而不是塞进裁决器, 是为了让"矩阵怎么算的"可回放:
裁决器只读结论, 不参与取数与判定.
"""

from __future__ import annotations

from ...agents.base import BaseAgent
from ...config import STRATEGY_NEWS_WINDOW_DAYS
from ..contracts import CrossMatrix
from ..rules import flow as flow_rules
from ..rules import matrix as matrix_rules
from ..rules import news as news_rules
from ..runtime import RunContext, StepResult


class CrossCheckAgent(BaseAgent):
    agent_name = "strategy_cross_check"

    def run(self, ctx: RunContext) -> StepResult:
        if not ctx.verdicts:
            return StepResult(self.agent_name, ok=False, degraded=True,
                              summary="无篮子, 跳过交叉验证")

        impacts, ev = ctx.port.news_impacts(days=STRATEGY_NEWS_WINDOW_DAYS)
        ctx.add_evidence(ev)

        names = [v.basket.name for v in ctx.verdicts]
        buckets = news_rules.group(impacts, names)

        ctx.matrix = _build(ctx, buckets)
        ctx.news_hits = news_rules.coverage(buckets)
        for name, items in buckets.items():
            window = news_rules.catalyst(items)
            if window:
                ctx.catalysts[name] = window

        self.log(
            decision="computed",
            reason=(
                f"news {len(impacts)} impacts / {ctx.news_hits} 命中篮子; "
                f"resonance {ctx.matrix.resonance_n}, divergence {ctx.matrix.divergence_n}"
            ),
        )
        if ctx.news_hits == 0:
            return StepResult(
                self.agent_name, ok=False, degraded=True,
                summary=f"新闻面无证据 ({len(impacts)} 条主题影响均未命中篮子), 矩阵最多两方对齐",
            )
        return StepResult(
            self.agent_name,
            summary=(
                f"矩阵 {len(ctx.matrix.rows)} 行 / 共振 {ctx.matrix.resonance_n} / "
                f"背离 {ctx.matrix.divergence_n} / 新闻证据 {ctx.news_hits} 条"
            ),
        )


def _build(ctx: RunContext, buckets: dict[str, list]) -> CrossMatrix:
    flow_sign = {s.basket_name: flow_rules.flow_sign(s) for s in ctx.shadows}
    news_sign = {name: news_rules.sign(items) for name, items in buckets.items()}
    sources = {
        v.basket.name: news_rules.independent_facts(
            buckets.get(v.basket.name, []), _tag_text(v),
        )
        for v in ctx.verdicts
    }
    return matrix_rules.build_matrix(
        ctx.verdicts, flow_sign=flow_sign, news_sign=news_sign, sources=sources,
    )


def _tag_text(verdict) -> str:
    """篮子的涨停归因原文, 用于识别"新闻复述归因"这类同源事实."""
    tags = verdict.basket.reason_tags or {}
    return "+".join(tag for values in tags.values() for tag in values)

