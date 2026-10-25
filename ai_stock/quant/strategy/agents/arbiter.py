"""裁决器 (M6 生成端).

分工: 矩阵与断言全部由纯规则算好; LLM 只负责把已定稿的事实写成判断与反驳.
这条分工是"LLM 不允许取数、也不允许改数"的落点 —— 一旦让模型参与判定,
同一份数据每天会得出不同结论, 记分体系就失效了.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime

from ...agents.base import BaseAgent
from ...calendar_utils import next_trading_day
from ...config import STRATEGY_MAX_CLAIMS
from ...llm_helper import structured_invoke
from ..contracts import ArbiterVerdict
from ..rules import claim as claim_rules
from ..runtime import RunContext, StepResult

_PROMPT = """你是 A 股首席策略官. 下面是**已经算好**的结构化事实.

硬约束:
1. 只做归纳与措辞, **不得引入输入中不存在的数字**, 不得新增板块或个股.
2. 必须给一句无条件方向判断, 不得用"若…则…"代替方向.
3. red_team 用 80 字以内写出最强反驳, 并说明"若我错了最可能错在哪".
4. 事件驱动为并购重组/借壳/易主的高标属个股独立事件, 不得写成板块启动.
5. news_coverage=0 时禁止声称"新闻面印证/消息面配合"; 新闻方向只能引用
   matrix 里给出的 news 文本, 不得自行判读新闻多空.

事实:
{payload}
"""


class ArbiterAgent(BaseAgent):
    agent_name = "strategy_arbiter"

    def run(self, ctx: RunContext) -> StepResult:
        self._claims(ctx)
        ctx.arbiter = self._verdict(ctx)

        good = ctx.matrix.resonance_n
        return StepResult(
            self.agent_name,
            summary=(
                f"矩阵 {len(ctx.matrix.rows)} 行 / 共振 {good} / 背离 "
                f"{ctx.matrix.divergence_n} / 断言 {len(ctx.claims)} 条"
            ),
        )

    # ---- 断言 --------------------------------------------------------------

    def _claims(self, ctx: RunContext) -> None:
        verify_date = next_trading_day(_as_date(ctx.trade_date))
        claims = claim_rules.make_claims(
            ctx.verdicts,
            ctx.traces,
            period=f"{ctx.trade_date}-P1",
            period_no=1,
            trade_date=ctx.trade_date,
            verify_date=verify_date,
            regime=ctx.regime,
        )
        ctx.claims = claims[:STRATEGY_MAX_CLAIMS]

    # ---- 裁决 --------------------------------------------------------------

    def _verdict(self, ctx: RunContext) -> ArbiterVerdict:
        payload = self._payload(ctx)
        fallback = self._fallback(ctx)
        # 三道闸: 显式关闭 / LLM 不可用 / 事实为空 —— 任一命中都走规则版.
        # 规则版永远可用, 所以"LLM 挂了"不构成一次运行失败.
        if not _llm_allowed() or not self.llm.available or not ctx.verdicts:
            return fallback
        return structured_invoke(
            self.llm.pick(deep=True),
            ArbiterVerdict,
            _PROMPT.format(payload=payload),
            default=fallback,
            deep=True,
        )

    def _payload(self, ctx: RunContext) -> str:
        data = {
            "trade_date": ctx.trade_date,
            "regime": {
                "code": ctx.regime.code,
                "growth_cap": ctx.regime.growth_cap,
                "note": ctx.regime.note,
            },
            "baskets": [
                {
                    "name": v.basket.name,
                    "synth_pct": v.synth_change_pct,
                    "valid_n": v.valid_n,
                    "cap": v.rating_cap,
                    "scope": v.verdict,
                    "catalyst": ctx.catalysts.get(v.basket.name, ""),
                    "reason": v.reason,
                }
                for v in ctx.verdicts[:6]
            ],
            "leaders": [
                {
                    "name": t.name,
                    "boards": t.boards,
                    "driver": t.real_driver,
                    "attribution": t.attribution,
                    "fund": t.fund_verdict,
                }
                for t in ctx.traces[:5]
            ],
            "matrix": [
                {
                    "basket": r.basket_name,
                    "consistency": r.consistency,
                    "flow": r.flow_view,
                    "news": r.news_view,
                    "pseudo": r.pseudo_resonance,
                }
                for r in ctx.matrix.rows[:6]
            ],
            "news_coverage": ctx.news_hits,
            "ladder": ctx.ladder_health,
        }
        return json.dumps(data, ensure_ascii=False)

    def _fallback(self, ctx: RunContext) -> ArbiterVerdict:
        """LLM 不可用时的规则版结论. 宁可少说, 不可编造."""
        tradable = [v for v in ctx.verdicts if v.valid_n >= 2]
        top = max(tradable, key=lambda v: v.synth_change_pct, default=None)
        if top is not None:
            mainline = f"{top.basket.name}({top.rating_cap})"
        elif ctx.verdicts:
            # 全是"个股事件"时必须说清楚, 不能让用户以为有可交易板块
            mainline = f"无(当日 {len(ctx.verdicts)} 个结构均为个股事件/观察)"
        else:
            mainline = "无明确主线"

        stage = "无主线"
        if top is not None:
            pct = top.synth_change_pct
            stage = "加速" if pct >= 5 else ("半程" if pct >= 2 else "起步")

        advice = (
            [f"关注 {mainline}, 仅在合成涨幅与资金同向时参与"]
            if top is not None
            else ["本期无可交易板块, 建议观望; 个股事件不构成建仓依据"]
        )
        return ArbiterVerdict(
            headline=(
                f"{ctx.trade_date} 结构: {mainline}; "
                f"梯队{ctx.ladder_health}; regime={ctx.regime.code}"
            ),
            mainline=mainline,
            stage=stage,  # type: ignore[arg-type]
            red_team="规则版无红队段落 (LLM 不可用). 最可能错在样本与已定价判断.",
            advice=advice,
            risks=[ctx.regime.note] if ctx.regime.note else [],
        )


def _as_date(trade_date: str) -> date:
    try:
        return datetime.strptime(trade_date, "%Y-%m-%d").date()
    except ValueError:
        return date.today()


def _llm_allowed() -> bool:
    """运行时可关: STRATEGY_DESK_LLM=0.

    测试与离线环境必须能关掉 —— 否则一次 LLM 请求会把单测拖到超时,
    而这正是项目曾经吃过的亏 (无超时 invoke 拖到 3600s 熔断).
    """
    return os.getenv("STRATEGY_DESK_LLM", "1").strip().lower() not in ("0", "false", "no")
