"""报告渲染 —— JSON → 三段式.

无 LLM: 模板拼接. 理由很实际 —— 报告结构固定为"大盘总览 / 关注板块 / 交易建议",
让模型来拼只会引入格式漂移, 而且每次跑出来的字段名都不一样, 前端没法写.
"""

from __future__ import annotations

from .contracts import BasketVerdict
from .runtime import RunContext

_SECTION_TITLES = ("大盘总览", "关注板块", "交易建议")


def build_sections(ctx: RunContext) -> dict:
    """生成三段式. 缺数据的段落显式写"数据缺失", 不留空也不编造."""
    arb = ctx.arbiter
    missing = {e.kind for e in ctx.missing_evidences}

    overview = [
        _item("当日小结", arb.headline if arb else "无可裁决结论"),
        _item("资金情况", _fund_text(ctx, missing)),
        _item("市场情绪", f"高标梯队: {ctx.ladder_health}; 涨停池样本 "
                         f"{_limit_up_n(ctx)} 只"),
        _item("大盘风格", ctx.style_label or "数据不足(防御端/成长端板块不足)"),
        _item("新闻面", _news_text(ctx)),
        _item("宏观状态", f"regime={ctx.regime.code}; {ctx.regime.note}"),
        _item("主线", _mainline_text(arb)),
    ]

    return {
        "as_of": ctx.trade_date,
        "snapshot_type": ctx.snapshot_type,
        "notice": _notice(ctx),
        "titles": list(_SECTION_TITLES),
        "overview": overview,
        "boards": [_board_row(v, ctx) for v in ctx.verdicts],
        "leaders": [_leader_row(t) for t in ctx.traces],
        "matrix": [r.model_dump() for r in ctx.matrix.rows],
        "advice": list(arb.advice) if arb else [],
        "risks": list(arb.risks) if arb else [],
        "red_team": arb.red_team if arb else "",
        "style": ctx.style_label,
        "news_hits": ctx.news_hits,
        "ladder": ctx.ladder_health,
        "regime": ctx.regime.code,
        "degraded": ctx.degraded_steps,
        "missing": sorted(missing),
    }


# ---- 段落 ------------------------------------------------------------------


def _item(key: str, value: str) -> dict:
    return {"k": key, "v": value}


def _fund_text(ctx: RunContext, missing: set[str]) -> str:
    if "board_flow" in missing:
        return "数据缺失(板块资金流不可用), 本期不给资金结论"
    usable = [s for s in ctx.shadows if s.pattern != "数据缺失"]
    if not usable:
        return "影子板块资金序列不可用"
    top = max(usable, key=lambda s: abs(s.d0 or 0.0))
    return f"{top.basket_name} {top.pattern}(D0={top.d0:.0f}万), 共 {len(usable)} 条序列"


def _news_text(ctx: RunContext) -> str:
    """新闻面必须显式报"参与度": 0 条时读者不能以为矩阵已三方对齐."""
    if ctx.news_hits == 0:
        missing = "news_impacts" in {e.kind for e in ctx.missing_evidences}
        reason = "新闻榜不可用" if missing else "近期无命中篮子的主题级方向"
        return f"{reason}, 本期矩阵最多两方对齐"
    resonance = ctx.matrix.resonance_n
    return (
        f"{ctx.news_hits} 条主题证据参与交叉验证; "
        f"共振 {resonance} / 背离 {ctx.matrix.divergence_n}"
    )


def _limit_up_n(ctx: RunContext) -> int:
    for e in ctx.evidences:
        if e.kind == "limit_up" and e.ok:
            return int(e.payload.get("n", 0))
    return 0


def _mainline_text(arb) -> str:
    if arb is None:
        return "无"
    if arb.stage == "无主线":
        return arb.mainline          # 避免 "无(...)(无主线)" 这种自我重复
    return f"{arb.mainline}({arb.stage})"


def _notice(ctx: RunContext) -> str:
    if ctx.snapshot_type == "intraday":
        return "⚠️ 盘中快照, 结论未定局; 断言不入账"
    return "收盘定稿"


def _board_row(v: BasketVerdict, ctx: RunContext | None = None) -> dict:
    """一行板块. 催化窗口与龙头来自新闻榜与已溯源高标, 缺就写"—"."""
    calendar = (ctx.catalysts.get(v.basket.name, "") if ctx else "") or "—(无新闻催化)"
    return {
        "name": v.basket.name,
        "parent": v.basket.parent_board,
        "driver": _dominant(v),
        "heat": f"{v.rating_cap}(有效样本 {v.valid_n})",
        "scope": v.verdict,
        "synth_pct": v.synth_change_pct,
        "stage": _stage(v),
        "basis": v.reason,
        "calendar": calendar,
        "leaders": _basket_leaders(v, ctx),
    }


def _basket_leaders(v: BasketVerdict, ctx: RunContext | None) -> str:
    """篮子内已溯源的高标. 只列真正在篮子里且有连板记录的标的."""
    if ctx is None:
        return ""
    members = set(v.basket.members)
    hits = [t for t in ctx.traces if t.symbol in members]
    return "、".join(f"{t.name}({t.board_type})" for t in hits[:3])


def _leader_row(t) -> dict:
    return {
        "symbol": t.symbol,
        "name": t.name,
        "boards": t.boards,
        "board_type": t.board_type,
        "driver": t.real_driver,
        "attribution": t.attribution,
        "fund": t.fund_verdict,
        "warn": "" if t.extrapolable else "个股独立事件, 不可外推为板块",
    }


def _dominant(v: BasketVerdict) -> str:
    counts: dict[str, int] = {}
    for s in v.basket.members:
        d = v.basket.driver.get(s, "情绪")
        counts[d] = counts.get(d, 0) + 1
    return max(counts, key=counts.get) if counts else "情绪"


def _stage(v: BasketVerdict) -> str:
    if v.synth_change_pct >= 6:
        return "加速"
    if v.synth_change_pct >= 3:
        return "主升"
    if v.synth_change_pct >= 0:
        return "启动"
    return "技术性调整"
