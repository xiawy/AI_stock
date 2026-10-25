"""运行期上下文.

只承载"这一步产出了什么", 不做判断、不碰 IO. 放在单独模块是为了让
``contracts`` 保持纯数据、``rules`` 保持零依赖.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .contracts import (
    ArbiterVerdict,
    BasketVerdict,
    BoardFlow,
    Claim,
    CrossMatrix,
    Evidence,
    LeaderTrace,
    Narrative,
    Regime,
    ShadowSeries,
)


@dataclass
class StepResult:
    """单步结果. degraded=True 表示"跑完了但产出不可用", 与异常区分开."""

    name: str
    ok: bool = True
    degraded: bool = False
    summary: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name, "ok": self.ok,
            "degraded": self.degraded, "summary": self.summary,
        }


@dataclass
class RunContext:
    trade_date: str
    snapshot_type: str
    port: object

    evidences: list[Evidence] = field(default_factory=list)
    boards: list[BoardFlow] = field(default_factory=list)
    verdicts: list[BasketVerdict] = field(default_factory=list)
    traces: list[LeaderTrace] = field(default_factory=list)
    shadows: list[ShadowSeries] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    narratives: dict[str, Narrative] = field(default_factory=dict)

    regime: Regime = field(default_factory=Regime)
    matrix: CrossMatrix = field(default_factory=CrossMatrix)
    arbiter: Optional[ArbiterVerdict] = None
    style_label: str = ""
    ladder_health: str = "无高标"

    # 篮子 → 催化窗口文本 (来自新闻榜 horizon/预期区间). 空字典表示无新闻证据,
    # 前端显示"—"而不是编一个日期.
    catalysts: dict[str, str] = field(default_factory=dict)
    # 命中的新闻主题条数: 0 表示本期新闻面完全缺位, 矩阵只能"证据不足"
    news_hits: int = 0

    steps: list[StepResult] = field(default_factory=list)

    def add_evidence(self, ev: Evidence) -> None:
        self.evidences.append(ev)

    def absorb(self, result: StepResult) -> None:
        self.steps.append(result)

    @property
    def degraded_steps(self) -> list[str]:
        return [s.name for s in self.steps if s.degraded or not s.ok]

    @property
    def missing_evidences(self) -> list[Evidence]:
        return [e for e in self.evidences if not e.ok]
