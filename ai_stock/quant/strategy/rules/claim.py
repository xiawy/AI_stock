"""断言生成与四态判定 (M6, PRD §7.6).

为什么是四态而不是三态: 数据故障与判断错误必须分开. 把一次接口封禁判成
"证伪", 会让整周权重被错误下调 —— 而系统不会报任何错.
"""

from __future__ import annotations

from datetime import date
from typing import Sequence

from ..contracts import (
    BasketVerdict,
    Claim,
    Direction,
    Judgement,
    LeaderTrace,
    PriceBar,
    Regime,
)


def make_claims(
    verdicts: Sequence[BasketVerdict],
    traces: Sequence[LeaderTrace],
    *,
    period: str,
    period_no: int,
    trade_date: str,
    verify_date: date,
    regime: Regime,
) -> list[Claim]:
    """从当日结论生成可证伪断言.

    只对有证据等级的篮子出断言: 有效样本 <2 的"个股事件"不进记分 ——
    那不是板块判断, 记进去只会污染分组胜率.
    """
    claims: list[Claim] = []
    regime_code = regime.code

    mainline = [v for v in verdicts if v.valid_n >= 2]
    for idx, v in enumerate(mainline, start=1):
        direction = _direction(v.synth_change_pct)
        claims.append(Claim(
            claim_id=f"P{period_no}-M{idx}",
            period=period,
            type="MAINLINE",
            target=v.basket.name,
            driver=_dominant_driver(v),
            text=(
                f"{v.basket.name} 评级上限{v.rating_cap}"
                f"(有效样本 {v.valid_n}, 合成 {v.synth_change_pct:+.2f}%)"
            ),
            direction=direction,
            horizon="T+1",
            confidence=_confidence(v),
            falsify=_falsify_mainline(v, direction),
            magnitude=_magnitude(v),
            regime=regime_code,
            crowding=_crowding(v),
            verify_date=verify_date,
        ))

    for idx, t in enumerate(traces, start=1):
        claims.append(Claim(
            claim_id=f"P{period_no}-L{idx}",
            period=period,
            type="LEADER",
            target=f"{t.name}({t.symbol})",
            driver=t.real_driver,
            text=(
                f"{t.board_type}高标, 归属={t.attribution}, "
                f"资金={t.fund_verdict}, 可外推={t.extrapolable}"
            ),
            direction="震荡",
            horizon="T+2",
            confidence="中",
            falsify="若其后 2 日出现 ≥2 只同类个股涨停且所属板块放量, 则本判定失效",
            magnitude="-6%~+6%",
            regime=regime_code,
            crowding=">70%",
            verify_date=verify_date,
        ))

    return claims


def judge(claim: Claim, bars: Sequence[PriceBar], asof: date) -> Judgement:
    """四态判定.

    红线: ``bar_date`` 不匹配或 ``provisional=True`` 一律不进 judged ——
    用盘中价回填等于把"未定局"当定局, 是记分体系最常见的污染源.
    """
    if asof < claim.verify_date:
        return Judgement(state="pending")

    bar = next((b for b in bars if b.bar_date == claim.verify_date.isoformat()), None)
    if bar is None:
        # 当日取不到 → 可能还没出, 也可能停牌. 按时间区分, 不按猜测区分.
        if asof == claim.verify_date:
            return Judgement(state="pending")
        return Judgement(state="unjudged", reason="日线缺失/停牌")
    if bar.provisional:
        return Judgement(state="pending", reason="日线未完成")

    return Judgement(
        state="judged",
        outcome=_outcome(claim.direction, bar.pct_change),
        move=round(bar.pct_change, 2),
    )


# ---- 内部 ----------------------------------------------------------------


def _direction(pct: float) -> Direction:
    if pct > 1.0:
        return "上行"
    if pct < -1.0:
        return "下行"
    return "震荡"


def _dominant_driver(v: BasketVerdict) -> str:
    counts: dict[str, int] = {}
    for s in v.basket.members:
        d = v.basket.driver.get(s, "情绪")
        counts[d] = counts.get(d, 0) + 1
    return max(counts, key=counts.get) if counts else "情绪"  # type: ignore[return-value]


def _confidence(v: BasketVerdict) -> str:
    if v.valid_n >= 8 and v.driver_consistency >= 0.6:
        return "中高"
    if v.valid_n >= 5:
        return "中"
    return "低"


def _crowding(v: BasketVerdict) -> str:
    if v.synth_change_pct >= 4.0:
        return ">70%"
    if v.synth_change_pct <= 0.0:
        return "<30%"
    return "30-70%"


def _magnitude(v: BasketVerdict) -> str:
    if v.rating_cap == "强":
        return "-4%~+8%"
    if v.rating_cap == "中":
        return "-3%~+5%"
    return "-5%~+3%"


def _falsify_mainline(v: BasketVerdict, direction: Direction) -> str:
    if direction == "上行":
        return "次日该篮子合成涨跌幅 ≤ 0 视为证伪"
    if direction == "下行":
        return "次日该篮子合成涨跌幅 > +1% 视为证伪"
    return "次日该篮子合成涨跌幅绝对值 > 3% 视为方向判定失效"


def _outcome(direction: Direction, move: float) -> str:
    """判定与幅度双阈值: 方向对但幅度微小算"部分", 不白给兑现."""
    if direction == "上行":
        if move >= 1.0:
            return "兑现"
        return "部分" if move > 0 else "证伪"
    if direction == "下行":
        if move <= -1.0:
            return "兑现"
        return "部分" if move < 0 else "证伪"
    # 震荡: ±2% 内算兑现
    if abs(move) <= 2.0:
        return "兑现"
    return "部分" if abs(move) <= 4.0 else "证伪"
