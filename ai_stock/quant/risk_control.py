"""Risk control subsystem (设计文档 §11).

两层风控:
1. 同步前置校验 ``pre_trade_check`` — 下单前必须调用, 所有硬性规则同步
   返回 ok / reject+reason. 交易第一道防线.
2. 异步风控 Agent (agents/risk.py) — Redis 队列兜底扫描, 发现已持仓违反
   规则 → 生成强制调仓任务投 hold 队列; 账户级指标触发冻结交易开关.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import db_ops
from .broker import OrderSide, get_broker
from .config import (
    BUY_POSITION_RATIO,
    INDUSTRY_CONCENTRATION_MAX,
    MARKET_TIMING_ENABLED,
    MAX_HOLDING_COUNT,
    NEW_LISTING_MIN_BARS,
    POSITION_RATIO_HARD_CAP,
    SINGLE_POSITION_MAX_PCT,
)
from .data_service import get_data_service

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    ok: bool
    reason: str = ""
    details: dict | None = None

    def to_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "details": self.details or {}}


def account_snapshot() -> dict:
    """账户快照: 现金/市值/总资产/回撤/当日盈亏."""
    broker = get_broker()
    try:
        info = broker.get_account_info()
    except Exception as exc:
        logger.warning("account snapshot failed: %s", exc)
        return {"available_cash": 0.0, "market_value": 0.0,
                "total_assets": 0.0, "drawdown_pct": 0.0, "daily_pnl": 0.0}
    initial = float(db_ops.get_config_value("initial_cash", "1000000"))
    drawdown = 0.0
    peak = max(initial, info.total_assets)  # 简化峰值: 初始资金 vs 当前
    if peak > 0:
        drawdown = (peak - info.total_assets) / peak
    return {
        "available_cash": info.available_cash,
        "market_value": info.total_market_value,
        "total_assets": info.total_assets,
        "drawdown_pct": round(drawdown, 4),
        "daily_pnl": round(db_ops.get_realized_pnl_today(), 2),
    }


def pre_trade_check(
    order,
    snapshot: Optional[dict] = None,
    position_ratio: Optional[float] = None,
) -> CheckResult:
    """下单前同步前置校验 (第一道防线, §11.1).

    Args:
        order: broker.OrderRequest
        snapshot: 可选预取的账户快照 (省一次 broker 调用)
        position_ratio: 本次下单实际使用的资金比例 (含动态仓位系数)。
            P1-1: 原实现把上限硬编码成 ``available_cash × 20%``, 而建仓侧按
            「20% × coef(最高 1.2)」下单 → dyn_confidence ≥ 0.8 且 stage=BODY
            的高置信标的 **必然** 超过上限而被拒, 与「宁缺毋滥」的产品意图相反。
            现在上限与实际下单比例共用同一事实来源; 未传入时用基础比例兜底。
    """
    snap = snapshot or account_snapshot()
    details = {"snapshot": snap}

    # 1. 全局总开关 — 一键紧急熔断, 无需重启进程 (§14.5)
    if db_ops.get_config_value("global_trade_enable", "1") != "1":
        return CheckResult(False, "全局交易开关关闭 (global_trade_enable=0)", details)

    # 2. 账户级风控冻结 (总回撤 / 单日亏损, 只允许卖出)
    if order.side == OrderSide.BUY:
        if db_ops.get_config_value("trade_frozen", "0") == "1":
            return CheckResult(False, "账户风控冻结中, 仅允许卖出", details)
        max_dd = float(db_ops.get_config_value("max_drawdown_pct", "0.20"))
        if snap.get("drawdown_pct", 0.0) >= max_dd:
            _freeze("总回撤超阈值", snap)
            return CheckResult(False, f"总回撤 {snap['drawdown_pct']:.1%} 超阈值 {max_dd:.0%}", details)
        max_daily = float(db_ops.get_config_value("max_daily_loss_pct", "0.03"))
        initial = float(db_ops.get_config_value("initial_cash", "1000000"))
        if initial > 0 and snap.get("daily_pnl", 0.0) < -initial * max_daily:
            _freeze("单日亏损超阈值", snap)
            return CheckResult(False, f"单日亏损 {snap['daily_pnl']} 超阈值", details)

    # 3. 单日最大委托笔数 / 单笔最大金额 / 白名单
    max_orders = int(db_ops.get_config_value("max_daily_orders", "50"))
    if db_ops.count_orders_today() >= max_orders:
        return CheckResult(False, f"超过单日最大委托笔数 {max_orders}", details)
    try:
        quote = get_data_service().get_realtime_quote(order.symbol)
    except Exception as exc:
        logger.warning("pre_trade_check quote fetch failed for %s: %s", order.symbol, exc)
        quote = None
    if not quote:
        return CheckResult(False, f"无法获取 {order.symbol} 行情, 拒绝下单", details)
    details["quote"] = quote
    max_value = float(db_ops.get_config_value("max_order_value", "250000"))
    est_amount = quote["price"] * order.quantity
    if est_amount > max_value:
        return CheckResult(
            False, f"单笔金额 {est_amount:.0f} 超上限 {max_value}", details,
        )
    whitelist = [
        s.strip() for s in db_ops.get_config_value("allowed_symbols", "").split(",")
        if s.strip()
    ]
    if whitelist and order.symbol not in whitelist:
        return CheckResult(False, f"{order.symbol} 不在交易白名单", details)

    # 4. A 股交易约束
    if order.side == OrderSide.SELL:
        holding = db_ops.get_holding(order.symbol)
        if holding and holding.get("cannot_sell_until"):
            until = holding["cannot_sell_until"]
            try:
                until_dt = datetime.fromisoformat(until)
                if until_dt.tzinfo is None:
                    # 旧数据为北京时间 naive 写入, 按市场时区解释 (当 UTC 会多锁 8 小时)
                    until_dt = until_dt.replace(tzinfo=timezone(timedelta(hours=8)))
                if until_dt > datetime.now(timezone.utc):
                    return CheckResult(False, f"T+1: {until} 前禁止卖出", details)
            except ValueError:
                pass
        limit_down = quote.get("limit_down", 0)
        if limit_down and quote["price"] <= limit_down:
            return CheckResult(False, f"跌停价 {limit_down} 不可卖出", details)
    else:
        if quote.get("limit_up", 0) and quote["price"] >= quote["limit_up"]:
            return CheckResult(False, f"涨停价 {quote['limit_up']} 不可买入", details)

        # 4.1 标的资格硬过滤 (T9): ST/退市 / 停牌 / 次新股
        blocked = _symbol_eligibility(order.symbol, quote)
        if blocked:
            return CheckResult(False, blocked, details)

        holdings = db_ops.get_holdings()
        # 5. 个股层: 持仓数上限 / 单次建仓比例
        if order.symbol not in {h["symbol"] for h in holdings}:
            if len(holdings) >= MAX_HOLDING_COUNT:
                return CheckResult(
                    False, f"持仓数已达上限 {MAX_HOLDING_COUNT} 支", details,
                )

        # 5.1 行业集中度硬约束 (T2): 同行业最多 N 只
        concentration = _industry_concentration_check(order.symbol, holdings)
        if concentration:
            return CheckResult(False, concentration, details)

        # 5.2 大盘择时门 (T3): 沪深300 走弱时只减不加
        timing = _market_timing_check()
        if timing:
            return CheckResult(False, timing, details)

        # 5.3 单次建仓金额上限: 与建仓侧同一比例口径
        ratio = position_ratio if position_ratio is not None else BUY_POSITION_RATIO
        ratio = max(0.0, min(float(ratio), POSITION_RATIO_HARD_CAP * 2))
        budget = snap.get("available_cash", 0.0) * ratio
        # 1‰ 容差: 下单侧按「价格×1.002 缓冲」定量, 极易在边界上超 1~2 元
        if est_amount > budget * 1.001:
            return CheckResult(
                False,
                f"单次建仓金额 {est_amount:.0f} 超过可用资金 {ratio:.0%} ({budget:.0f})",
                details,
            )

    return CheckResult(True, "passed", details)


def _symbol_eligibility(symbol: str, quote: dict) -> str:
    """建仓标的资格过滤 (T9). 返回拒绝原因, 通过返回空串.

    - ST/*ST/退市风险: 名称含 ST / 退
    - 停牌: 现价 ≤ 0 (行情接口给出 0 或缺失)
    - 次新股: 上市不足 ``NEW_LISTING_MIN_BARS`` 个交易日 (K 线根数不足)
    任一数据不可得时 **不拦截** (数据故障不应变成交易开关)。
    """
    name = str(quote.get("name") or "")
    if "ST" in name.upper() or "退" in name:
        return f"{symbol} {name} 属 ST/退市风险标的, 不建仓"
    price = float(quote.get("price") or 0)
    if price <= 0:
        return f"{symbol} 现价 {price} 异常 (疑似停牌), 拒绝下单"
    try:
        df = get_data_service().get_ohlcv(symbol, lookback_days=NEW_LISTING_MIN_BARS + 10)
    except Exception:
        return ""
    if df is None or len(df) == 0:
        return ""
    if len(df) < NEW_LISTING_MIN_BARS:
        return f"{symbol} 上市仅 {len(df)} 个交易日 (<{NEW_LISTING_MIN_BARS}), 次新股不建仓"
    return ""


def _industry_concentration_check(symbol: str, holdings: list[dict]) -> str:
    """行业集中度硬约束 (T2): 同一行业已持仓数达上限则拒绝.

    行业归口优先取持仓记录, 回退到自选池 ``industry``; 两者都缺失时跳过该约束
    (宁可放过, 不因元数据缺失阻断交易)。
    """
    if INDUSTRY_CONCENTRATION_MAX <= 0 or not holdings:
        return ""
    target = _resolve_industry(symbol)
    if not target:
        return ""
    same = [
        h for h in holdings
        if h.get("symbol") != symbol
        and _same_industry(_resolve_industry(h.get("symbol", ""), h), target)
    ]
    if len(same) >= INDUSTRY_CONCENTRATION_MAX:
        names = ",".join(h.get("symbol", "") for h in same[:3])
        return (
            f"行业集中度超限: 已持有 {target} 行业 {len(same)} 只 ({names}), "
            f"上限 {INDUSTRY_CONCENTRATION_MAX}"
        )
    return ""


def _resolve_industry(symbol: str, holding: Optional[dict] = None) -> str:
    if not symbol:
        return ""
    if holding and holding.get("industry"):
        return str(holding["industry"])
    try:
        pool = db_ops.get_optional_stock(symbol) or {}
    except Exception:
        return ""
    return str(pool.get("industry") or "")


def _same_industry(left: str, right: str) -> bool:
    """行业名宽松匹配: 板块命名在东财/新浪间存在粒度差异 (如 "军工" vs "航天航空")."""
    if not left or not right:
        return False
    return left == right or left in right or right in left


def _market_timing_check() -> str:
    """大盘择时门 (T3). 返回拒绝原因, 通过返回空串.

    - 开关走**运行时配置** ``market_timing_enable`` (默认取常量
      ``MARKET_TIMING_ENABLED``): 运维在配置界面拨开关立即生效, 与
      ``global_trade_enable`` 同一套「无需重启」语义。
    - 数据不可用 (regime='unknown') 时不拦截 —— 数据故障不应变成交易开关。
    """
    default = "1" if MARKET_TIMING_ENABLED else "0"
    enabled = str(db_ops.get_config_value("market_timing_enable", default)).strip().lower()
    if enabled not in ("1", "true", "yes", "on"):
        return ""

    try:
        from .market_regime import get_market_regime

        regime = get_market_regime()
    except Exception as exc:
        logger.warning("market timing check degraded: %s", exc)
        return ""
    if not regime.get("ok") or regime.get("regime") != "risk_off":
        return ""
    return f"大盘择时门: {regime.get('reason', '市场走弱')} → 只减不加"


def _freeze(reason: str, snap: dict) -> None:
    """账户级风控触发 → 冻结交易 (只允许卖出) + CRITICAL 告警."""
    from .ops_monitor import CRITICAL, get_alerts

    db_ops.set_config_value("trade_frozen", "1")
    get_alerts().emit(
        CRITICAL, "risk_freeze",
        f"账户全局风控触发, 冻结全部新建仓: {reason} (snapshot={snap})",
    )
    db_ops.log_decision(
        agent="risk_control", decision="freeze_account",
        reason=reason, detail=snap,
    )


def unfreeze_account() -> None:
    db_ops.set_config_value("trade_frozen", "0")
    db_ops.log_decision(agent="risk_control", decision="unfreeze_account", reason="人工解除冻结")


# ---------------------------------------------------------------------------
# 异步风控扫描逻辑 (由 agents/risk.py 的 risk_scan handler 调用)
# ---------------------------------------------------------------------------

def async_risk_scan() -> dict:
    """兜底扫描全部持仓 (§11.2): 违规 → 强制调仓任务; 账户指标 → 冻结.

    只做兜底, 不能作为交易第一道防线 (第一道是 pre_trade_check).
    """
    from .mq import enqueue_task
    from .config import HOLD_QUEUE
    from .ops_monitor import ERROR, get_alerts

    findings: list[dict] = []
    snap = account_snapshot()
    holdings = db_ops.get_holdings()
    broker = get_broker()
    total_assets = snap.get("total_assets", 0.0) or 0.0

    positions = {p.symbol: p for p in broker.get_positions()}
    # 总资产不可用 (行情/账户接口异常) 时跳过个股占比校验, 避免 0 除产生
    # 错误占比 (value_pct → inf) 触发误减仓
    if total_assets > 0:
        for holding in holdings:
            symbol = holding["symbol"]
            pos = positions.get(symbol)
            if pos is None:
                continue
            # 个股仓位占比
            value_pct = (pos.market_value or 0.0) / total_assets
            if value_pct > SINGLE_POSITION_MAX_PCT * 1.5:
                task_id, created = enqueue_task(
                    HOLD_QUEUE, "force_reduce",
                    {
                        "symbol": symbol,
                        "reason": f"单只仓位占比 {value_pct:.0%} 超限",
                        "target_pct": SINGLE_POSITION_MAX_PCT,
                    },
                    idempotent_key=f"force_reduce_{symbol}_{datetime.now().strftime('%Y%m%d%H%M')}",
                    priority=5,
                )
                if created:
                    findings.append({"symbol": symbol, "issue": "position_cap", "task_id": task_id})
                    get_alerts().emit(
                        ERROR, "position_cap",
                        f"{symbol} 仓位占比 {value_pct:.0%} 超限, 已生成强制减仓任务",
                    )

    # 账户级: 总回撤 / 单日亏损 → 冻结新建仓 (与 pre_trade_check 阈值一致)
    def _is_frozen() -> bool:
        return db_ops.get_config_value("trade_frozen", "0") == "1"

    max_dd = float(db_ops.get_config_value("max_drawdown_pct", "0.20"))
    if not _is_frozen() and snap.get("drawdown_pct", 0.0) >= max_dd:
        _freeze("异步风控: 总回撤超阈值", snap)
        findings.append({"issue": "drawdown_freeze"})

    max_daily = float(db_ops.get_config_value("max_daily_loss_pct", "0.03"))
    initial = float(db_ops.get_config_value("initial_cash", "1000000"))
    if not _is_frozen() and initial > 0 and snap.get("daily_pnl", 0.0) < -initial * max_daily:
        _freeze("异步风控: 单日亏损超阈值", snap)
        findings.append({"issue": "daily_loss_freeze"})

    # 单日委托笔数超限: 后续买单会被 pre_trade_check 拒绝, 这里显式告警留痕
    max_orders = int(db_ops.get_config_value("max_daily_orders", "50"))
    orders_today = db_ops.count_orders_today()
    if orders_today >= max_orders:
        findings.append({"issue": "daily_orders_over_limit", "max": max_orders})
        get_alerts().emit(
            ERROR, "daily_orders_over_limit",
            f"当日委托笔数 {orders_today} 已达上限 {max_orders}, 后续买单将被拒绝",
        )

    return {"findings": findings, "snapshot": snap}
