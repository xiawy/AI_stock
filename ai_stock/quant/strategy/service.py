"""策略台编排.

一次运行 = 一条固定顺序的流程. 单步失败不终止 (降级继续), 因为
"缺一块的报告"远比"今天什么都没有"有用 —— 后者会让记分体系出现空洞,
而系统性偏差最容易藏在空洞里.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Optional

from .. import db_ops
from ..calendar_utils import get_trade_date
from ..config import (
    STRATEGY_PROMPT_VERSION,
    STRATEGY_RUN_AT,
)
from .agents import (
    ArbiterAgent,
    CrossCheckAgent,
    FlowShadowAgent,
    LeaderTraceAgent,
    MacroGateAgent,
    SectorStructureAgent,
)
from .contracts import PriceBar
from .render import build_sections
from .repo import StrategyRepo
from .rules import claim as claim_rules
from .rules import regime as regime_rules
from .runtime import RunContext, StepResult

logger = logging.getLogger(__name__)

STEPS = (
    "sector_structure",   # M1 涨的是哪一拨
    "leader_trace",       # M2 高标能不能外推
    "flow_shadow",        # M3 资金进没进 + 风格
    "cross_check",        # M5 三条证据链对不对得上
    "macro_gate",         # M4 宏观收紧上限 (在矩阵之后, 只影响评级)
    "arbiter",            # M6 断言 + 措辞
)

_AGENTS = {
    "sector_structure": SectorStructureAgent,
    "leader_trace": LeaderTraceAgent,
    "flow_shadow": FlowShadowAgent,
    "cross_check": CrossCheckAgent,
    "macro_gate": MacroGateAgent,
    "arbiter": ArbiterAgent,
}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_strategy_flow(
    trade_date: Optional[str] = None,
    snapshot_type: str = "close",
    *,
    port=None,
    repo: Optional[StrategyRepo] = None,
) -> dict:
    """跑一次完整研判并落库. 返回摘要 (供 API / 日志 / 测试)."""
    trade_date = trade_date or str(get_trade_date())
    repo = repo or StrategyRepo()

    if snapshot_type == "intraday" and not _intraday_enabled():
        return {"ok": False, "skipped": "盘中快照未启用"}

    ctx = RunContext(
        trade_date=trade_date,
        snapshot_type=snapshot_type,
        port=port or _build_port(trade_date),
    )

    for name in STEPS:
        try:
            result = _AGENTS[name]().run(ctx)
        except Exception as exc:                  # 单步异常 → 降级, 不中断
            logger.exception("strategy step %s failed", name)
            result = StepResult(name, ok=False, degraded=True,
                                summary=f"{type(exc).__name__}: {str(exc)[:80]}")
        ctx.absorb(result)

    _apply_regime_cap(ctx)

    sections = build_sections(ctx)
    stats = repo.save_run(
        trade_date=trade_date,
        snapshot_type=snapshot_type,
        effective_from=_effective_from(trade_date, snapshot_type),
        evidences=ctx.evidences,
        verdicts=ctx.verdicts,
        traces=ctx.traces,
        shadows=ctx.shadows,
        regime=ctx.regime,
        claims=ctx.claims,
        sections=sections,
        degraded=ctx.degraded_steps,
        prompt_version=STRATEGY_PROMPT_VERSION,
    )

    summary = {
        "ok": True,
        "trade_date": trade_date,
        "snapshot_type": snapshot_type,
        "regime": ctx.regime.code,
        "style": ctx.style_label,
        "news_hits": ctx.news_hits,
        "boards": stats["baskets"],
        "leaders": stats["leaders"],
        "shadows": stats["shadows"],
        "claims": stats["claims"],
        "degraded": ctx.degraded_steps,
        "missing": [e.kind for e in ctx.missing_evidences],
    }
    logger.info("strategy desk done: %s", summary)
    return summary


def handle_strategy_run(task: dict) -> dict:
    """MQ handler: strategy_run."""
    payload = task.get("payload") or {}
    return run_strategy_flow(
        trade_date=payload.get("trade_date"),
        snapshot_type=payload.get("snapshot_type", "close"),
    )


def handle_strategy_coef_update(task: dict) -> dict:
    """MQ handler: strategy_coef_update (周频)."""
    return update_coefs((task.get("payload") or {}).get("as_of"))


# ---------------------------------------------------------------------------
# 回填与记分
# ---------------------------------------------------------------------------


def update_coefs(asof: Optional[str] = None, *, repo: Optional[StrategyRepo] = None) -> dict:
    """按 driver 胜率回算权重系数.

    只写 ``quant_system_config``, 不碰任何交易表. 系数是否被消费由
    ``strategy_reproduce_coef_enable`` 决定 —— 算与用分开, 才能先观察再放量.
    """
    from . import feedback

    asof = asof or str(get_trade_date())
    repo = repo or StrategyRepo()
    score = repo.score()
    coefs, skipped = feedback.compute_coefs(score.get("driver_quality") or {})
    payload = feedback.save_coefs(coefs, skipped, asof)
    logger.info(
        "strategy coef updated: %s (skipped %s, enabled=%s)",
        coefs, skipped, feedback.enabled(),
    )
    return {
        "ok": True,
        "as_of": asof,
        "coefs": coefs,
        "skipped": skipped,
        "enabled": feedback.enabled(),
        "judged": score.get("judged", 0),
        "updated_at": payload["updated_at"],
    }


def verify_due(asof: Optional[str] = None, *, port=None, repo: Optional[StrategyRepo] = None) -> dict:
    """回填到期断言.

    红线: 只用**已收盘**日线; 取不到 → unjudged (不进胜率分母).
    """
    asof = asof or str(get_trade_date())
    repo = repo or StrategyRepo()
    port = port or _build_port(asof)

    done, skipped = 0, 0
    for raw in repo.due_claims(asof):
        claim = _rehydrate(raw)
        if claim is None:
            skipped += 1
            continue
        bars = _bars_for(claim, repo, port)
        judgement = claim_rules.judge(claim, bars, _to_date(asof))
        if repo.apply_judgement(claim.claim_id, judgement):
            done += 1
        else:
            skipped += 1

    return {"ok": True, "as_of": asof, "verified": done, "skipped": skipped,
            "score": repo.score()}


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _build_port(trade_date: str):
    from .adapters import DataServiceAdapter

    return DataServiceAdapter(trade_date=trade_date)


def _apply_regime_cap(ctx: RunContext) -> None:
    """宏观只收紧不上调. 产业周期豁免未接线, 所以这里绝不放大评级."""
    for v in ctx.verdicts:
        capped = regime_rules.apply_cap(v.rating_cap, ctx.regime)
        if capped != v.rating_cap:
            v.reason = f"{v.reason}; 受 regime {ctx.regime.code} 约束上限 {capped}"
            v.rating_cap = capped


def _effective_from(trade_date: str, snapshot_type: str) -> str:
    """生效时点. 盘中快照永远不生效 —— 只有收盘报告能被交易链路消费."""
    if snapshot_type != "close":
        return ""
    from ..calendar_utils import next_trading_day

    nxt = next_trading_day(_to_date(trade_date))
    return f"{nxt.isoformat()} 09:15"


def _bars_for(claim, repo: StrategyRepo, port) -> list:
    """按断言类型取判定用的日线."""
    if claim.type == "MAINLINE":
        members = repo.basket_members(claim.period[:10], claim.target)
        return _basket_bar(members, claim.verify_date.isoformat(), port)

    m = re.search(r"\((\d{6})\)", claim.target)
    if not m:
        return []
    return port.bars(m.group(1), lookback_days=10)


def _basket_bar(members: list[str], verify_date: str, port) -> list[PriceBar]:
    """篮子日线: 成员当日涨跌幅等权平均, 任缺即视为不完整 (返回空)."""
    if not members:
        return []
    pcts: list[float] = []
    for symbol in members:
        hit = [b for b in port.bars(symbol, lookback_days=10) if b.bar_date == verify_date]
        if not hit:
            return []
        pcts.append(hit[0].pct_change)
    return [PriceBar(
        bar_date=verify_date,
        close=0.0,
        pct_change=round(sum(pcts) / len(pcts), 2),
        provisional=False,
    )]


def _rehydrate(raw: dict):
    """把库里的断言还原成契约对象 (只回填判定需要的字段)."""
    from .contracts import Claim

    try:
        return Claim(
            claim_id=raw["claim_id"],
            period=raw.get("period", ""),
            type=raw["type"],
            target=raw["target"],
            driver=raw.get("driver", "情绪"),
            direction=raw["direction"],
            verify_date=_to_date(raw["verify_date"]),
        )
    except Exception as exc:
        logger.warning("claim %s 还原失败: %s", raw.get("claim_id"), exc)
        return None


def _to_date(value) -> date:
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return date.today()


def _intraday_enabled() -> bool:
    return db_ops.get_config_value("strategy_intraday_enable", "0") == "1"


def should_run(now: Optional[datetime] = None) -> bool:
    """是否到点 (供调度器判断). 运行时开关优先."""
    if db_ops.get_config_value("strategy_desk_enable", "1") != "1":
        return False
    now = now or datetime.now()
    return (now.hour, now.minute) >= STRATEGY_RUN_AT
