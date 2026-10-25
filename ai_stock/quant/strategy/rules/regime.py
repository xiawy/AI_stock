"""宏观状态机 (M4, PRD §5).

唯一的硬约束: **只定权重与估值容忍度, 不定板块方向**.
缺失 ≥2 个变量 → ``growth_cap=None``, 语义是"不约束", 不是"不利".
把"不知道"降级成"不利", 会让一次接口故障把全市场成长板块集体打下来.

判定表是 v1 人工阈值, 不追求学术完备, 只保证:
① 可解释 (每条都有触发条件原文); ② 缺失时不猜; ③ 不产出个股结论.
"""

from __future__ import annotations

from ..contracts import MacroVars, RatingCap, Regime

# 阈值集中在此, 便于回测校准 (改阈值不需要动判定结构)
HIGH_10Y = 4.5
LOW_10Y = 3.0
HIGH_VIX = 30.0
LOW_VIX = 20.0
RICH_OIL = 85.0
HIGH_CPI = 3.0


def decide(v: MacroVars) -> Regime:
    missing = v.missing()
    if len(missing) >= 2:
        return Regime(
            code="未定",
            growth_cap=None,
            note=f"缺失 {len(missing)} 个变量({', '.join(missing)}), 本期不下调任何评级上限",
            vars=v,
            missing=missing,
        )

    code, cap, note = _classify(v)
    return Regime(
        code=code,
        growth_cap=cap,
        note=note,
        vars=v,
        missing=missing,
        triggers=_triggers(v),
    )


def apply_cap(proposed: RatingCap, regime: Regime) -> RatingCap:
    """把 regime 的评级上限作用到板块评级上.

    产业周期豁免未接线 (需要订单增速/能见度数据), 因此这里只做单向收紧:
    永不因为宏观把评级**上调** —— 那是产业逻辑的职责.
    """
    if regime.growth_cap is None:
        return proposed
    order = {"无": 0, "观察": 1, "中": 2, "强": 3}
    return proposed if order[proposed] <= order[regime.growth_cap] else regime.growth_cap


# ---- 内部 ----------------------------------------------------------------


def _classify(v: MacroVars) -> tuple[str, RatingCap, str]:
    if v.vix is not None and v.vix >= HIGH_VIX:
        return "R6", "中", f"VIX {v.vix:.0f} ≥ {HIGH_VIX:.0f}, 风险厌恶主导, 估值容忍度收紧"

    if (
        v.cpi_yoy is not None and v.cpi_yoy >= HIGH_CPI
        and v.oil is not None and v.oil >= RICH_OIL
        and v.real_yield is not None and v.real_yield > 0
    ):
        return "R2", "观察", (
            f"CPI {v.cpi_yoy:.1f}% + 油价 {v.oil:.0f} + 实际利率 {v.real_yield:.2f}% > 0, "
            "供给型通胀, 成长类评级上限压到观察"
        )

    if v.us10y is not None and v.us10y >= HIGH_10Y:
        return "R3", "中", f"美债 10Y {v.us10y:.2f}% ≥ {HIGH_10Y}%, 折现率压制高估值"

    if (
        v.us10y is not None and v.us10y <= LOW_10Y
        and (v.vix is None or v.vix < LOW_VIX)
    ):
        return "R1", "强", "利率低位 + 波动率温和, 成长类估值容忍度放开"

    return "R5", "中", "中性环境, 评级上限不额外收紧"


def _triggers(v: MacroVars) -> list[str]:
    """阈值型切换触发条件 —— 只写可观测的阈值, 不写"若情绪转好"这类废话."""
    out: list[str] = []
    if v.us10y is not None:
        out.append(f"美债 10Y 上破 {HIGH_10Y}% → 切 R3; 下破 {LOW_10Y}% → 切 R1")
    if v.vix is not None:
        out.append(f"VIX 上破 {HIGH_VIX:.0f} → 切 R6")
    if v.dr007 is not None:
        out.append("DR007 上破 2.0% → 国内流动性转紧, 小盘题材降权")
    return out
