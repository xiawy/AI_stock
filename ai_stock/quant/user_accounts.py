"""多用户模拟账户层 — 持仓池按用户隔离执行买卖.

架构:
- 引擎决策链路保持不变, 继续在 ``user_id='system'`` 系统账户上执行
  (SimulatedBroker 仅服务系统账户);
- 引擎成交后通过 :func:`fanout_buy` / :func:`fanout_sell` 扇出到每个
  活跃用户账户, 各账户独立校验资金/手数/持仓数后落库;
- 用户也可通过 backend API 手动买卖 (:func:`execute_user_buy` /
  :func:`execute_user_sell`).

起始资金配置:
- ``QUANT_INITIAL_CAPITAL`` (.env): 全员默认起始资金;
- ``QUANT_CAPITAL_FILE`` (JSON): 按用户覆盖, key 为 user_id 或 username,
  支持 ``"default"`` 键; 未列出用户用默认值. 默认路径
  ``<project_root>/backend/data/quant_capital.json``.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import db_ops
from .broker import OrderSide, calc_fee, round_lot
from .config import (
    BUY_POSITION_RATIO,
    CAPITAL_FILE_ENV,
    INITIAL_CAPITAL_ENV,
    LOT_SIZE,
    MAX_HOLDING_COUNT,
    SLIPPAGE_BPS,
)

logger = logging.getLogger(__name__)

DEFAULT_INITIAL_CAPITAL = 1_000_000.0
DEFAULT_CAPITAL_FILENAME = "quant_capital.json"

# A 股市场时区 (cannot_sell_until 写入/读取统一口径)
_MARKET_TZ = timezone(timedelta(hours=8))


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# 起始资金配置
# ---------------------------------------------------------------------------

def _capital_file_path() -> Path:
    path = os.getenv(CAPITAL_FILE_ENV, "").strip()
    if path:
        return Path(path)
    return _project_root() / "backend" / "data" / DEFAULT_CAPITAL_FILENAME


def _load_capital_overrides() -> dict:
    """读取按用户覆盖的起始资金配置 (文件不存在/损坏 → 空, 永不抛异常)."""
    path = _capital_file_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("资金配置文件 %s 解析失败, 忽略: %s", path, exc)
        return {}


def resolve_initial_capital(user_id: str, username: str = "") -> float:
    """解析用户起始资金: JSON 覆盖 (user_id > username > default) > env 默认."""
    try:
        default = float(os.getenv(INITIAL_CAPITAL_ENV, "").strip() or DEFAULT_INITIAL_CAPITAL)
    except ValueError:
        default = DEFAULT_INITIAL_CAPITAL
    overrides = _load_capital_overrides()
    for key in (str(user_id).strip(), (username or "").strip()):
        if key and key in overrides:
            try:
                return float(overrides[key])
            except (TypeError, ValueError):
                logger.warning("资金配置 %s=%r 非法, 回退默认", key, overrides[key])
    if "default" in overrides:
        try:
            return float(overrides["default"])
        except (TypeError, ValueError):
            pass
    return default


# ---------------------------------------------------------------------------
# 账户开户 / 快照
# ---------------------------------------------------------------------------

def get_or_create_account(user_id: str, username: str = "") -> Optional[dict]:
    """幂等开户: 已存在直接返回 (不动资金); 不存在按配置起始资金开户."""
    user_id = str(user_id).strip()
    if not user_id:
        return None
    existing = db_ops.get_user_account(user_id)
    if existing is not None:
        if username and existing.get("username") != username:
            db_ops.upsert_user_account({"user_id": user_id, "username": username})
            return db_ops.get_user_account(user_id) or existing
        return existing
    capital = resolve_initial_capital(user_id, username)
    db_ops.upsert_user_account({
        "user_id": user_id,
        "username": username,
        "initial_capital": capital,
        "cash_balance": capital,
    })
    logger.info("用户模拟账户已开通: %s (%s) 起始资金 %.2f", user_id, username, capital)
    return db_ops.get_user_account(user_id)


def _quote_price(symbol: str) -> tuple[float, str]:
    """实时行情价格 (失败返回 0); 返回 (价格, 名称)."""
    try:
        from .data_service import get_data_service

        quote = get_data_service().get_realtime_quote(symbol) or {}
        return float(quote.get("price") or 0.0), quote.get("name", "")
    except Exception as exc:
        logger.warning("fanout 行情获取失败 %s: %s", symbol, exc)
        return 0.0, ""


def user_portfolio_snapshot(user_id: str) -> dict:
    """用户账户快照: 现金 + 持仓市值(实时价, 降级成本价) + 总收益率."""
    account = db_ops.get_user_account(str(user_id).strip())
    if account is None:
        return {}
    holdings = db_ops.get_holdings(user_id)
    market_value = 0.0
    enriched = []
    for h in holdings:
        price, _ = _quote_price(h["symbol"])
        price = price or h.get("cost_price", 0.0)
        qty = int(h.get("quantity", 0) or 0)
        cost = float(h.get("cost_price", 0.0) or 0.0)
        mv = price * qty
        market_value += mv
        enriched.append({
            **h,
            "last_price": price,
            "market_value": round(mv, 2),
            "unrealized_pnl": round((price - cost) * qty, 2),
            "unrealized_pnl_pct": round((price - cost) / cost, 4) if cost > 0 else 0.0,
        })
    cash = float(account.get("cash_balance", 0.0) or 0.0)
    initial = float(account.get("initial_capital", 0.0) or 0.0)
    total = round(cash + market_value, 2)
    return {
        "account": account,
        "cash_balance": cash,
        "market_value": round(market_value, 2),
        "total_assets": total,
        "total_return": round(total - initial, 2),
        "total_return_pct": round((total - initial) / initial, 4) if initial > 0 else 0.0,
        "holdings": enriched,
    }


def _t1_lock_until() -> datetime:
    """T+1 禁卖截止时间: 下一交易日 15:00:01 (北京时间, 与引擎建仓口径一致).

    带时区写入: SQLite 持久化为 "+08:00" 偏移字符串, 读回即 aware,
    避免 naive 本地时间被读取侧误当 UTC 而延长禁卖窗口 8 小时.
    """
    from .calendar_utils import next_trading_day

    return datetime.combine(
        next_trading_day(datetime.now().date()), datetime.min.time(),
    ).replace(hour=15, tzinfo=_MARKET_TZ) + timedelta(seconds=1)


def _t1_active(holding: dict) -> bool:
    """持仓是否仍在 T+1 禁卖窗口内."""
    until = holding.get("cannot_sell_until")
    if not until:
        return False
    try:
        until_dt = datetime.fromisoformat(until) if isinstance(until, str) else until
    except ValueError:
        return False
    if until_dt.tzinfo is None:
        # 旧数据为北京时间 naive 写入, 按市场时区解释 (当 UTC 会多锁 8 小时)
        until_dt = until_dt.replace(tzinfo=_MARKET_TZ)
    return until_dt > datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 用户级买卖执行 (扇出与手动买卖共用)
# ---------------------------------------------------------------------------

def execute_user_buy(
    user_id: str,
    symbol: str,
    price: float = 0.0,
    name: str = "",
    quantity: int = 0,
    reason: str = "",
    plan: dict | None = None,
    stop_loss: float = 0.0,
    take_profit: float = 0.0,
    flow_id: str = "",
) -> dict:
    """在指定用户账户执行买入; price<=0 时自取行情 (含滑点).

    数量缺省 = 可用资金 × 20% 对应的整手; 校验持仓数上限与现金.
    """
    user_id = str(user_id).strip()
    account = get_or_create_account(user_id)
    if account is None:
        return {"ok": False, "reason": "账户创建失败"}
    if account.get("status") != "active":
        return {"ok": False, "reason": "账户已冻结, 仅允许卖出"}

    if price <= 0:
        price, quote_name = _quote_price(symbol)
        name = name or quote_name
        if price <= 0:
            return {"ok": False, "reason": f"无法获取 {symbol} 行情"}
        price = price * (1 + SLIPPAGE_BPS / 10000)  # 市价买入滑点

    cash = float(account.get("cash_balance", 0.0) or 0.0)
    holdings = db_ops.get_holdings(user_id)
    existing = next((h for h in holdings if h["symbol"] == symbol), None)
    if existing is None and len(holdings) >= MAX_HOLDING_COUNT:
        return {"ok": False, "reason": f"持仓数已达上限 {MAX_HOLDING_COUNT} 支"}

    if quantity > 0:
        qty = round_lot(int(quantity))
    else:
        budget = cash * BUY_POSITION_RATIO
        qty = round_lot(int(budget / price / LOT_SIZE) * LOT_SIZE)
    if qty <= 0:
        return {"ok": False, "reason": "预算不足一手 (100股)"}

    amount = round(price * qty, 2)
    fee = calc_fee(amount, OrderSide.BUY)
    if amount + fee > cash + 1e-6:
        return {"ok": False, "reason": f"可用资金不足 (需 {amount + fee:.2f}, 有 {cash:.2f})"}

    # ---- 成交: 扣款 + 持仓(均价摊薄) + 流水 ----
    db_ops.adjust_user_cash(user_id, -(amount + fee))
    cannot_sell_until = _t1_lock_until()
    if existing:
        old_qty = int(existing.get("quantity", 0) or 0)
        old_cost = float(existing.get("cost_price", 0.0) or 0.0)
        new_qty = old_qty + qty
        new_cost = (old_cost * old_qty + amount + fee) / new_qty if new_qty else 0.0
        # 加仓不重锁旧份额: 保留既有 cannot_sell_until (过期/缺失时才会延长),
        # 否则已解锁的旧仓位会被新仓的 T+1 窗口误冻结到下一交易日收盘后.
        old_lock = existing.get("cannot_sell_until")
        if isinstance(old_lock, str):
            try:
                old_lock = datetime.fromisoformat(old_lock)
            except ValueError:
                old_lock = None
        db_ops.upsert_holding({
            "symbol": symbol,
            "name": name or existing.get("name", ""),
            "quantity": new_qty,
            "available_quantity": int(existing.get("available_quantity", 0) or 0),
            "cost_price": round(new_cost, 4),
            "cannot_sell_until": old_lock or cannot_sell_until,
        }, user_id=user_id)
    else:
        db_ops.upsert_holding({
            "symbol": symbol,
            "name": name,
            "quantity": qty,
            "available_quantity": 0,  # T+1 冻结
            "cost_price": round((amount + fee) / qty, 4),
            "entry_reason": reason,
            "plan": plan or {},
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "cannot_sell_until": cannot_sell_until,
        }, user_id=user_id)

    order_id = f"USR-{uuid.uuid4().hex[:12]}"
    db_ops.add_trade_log({
        "user_id": user_id,
        "order_id": order_id,
        "symbol": symbol,
        "name": name,
        "side": "buy",
        "price": round(price, 3),
        "quantity": qty,
        "amount": amount,
        "fee": fee,
        "status": "filled",
        "reason": reason,
        "broker": "simulated",
    })
    db_ops.log_decision(
        agent="user_account", decision="buy_filled", symbol=symbol,
        flow_id=flow_id, reason=reason[:500],
        detail={"user_id": user_id, "quantity": qty, "price": round(price, 3)},
    )
    logger.info(
        "USER BUY %s %s ×%d @%.3f amount=%.2f fee=%.2f",
        user_id, symbol, qty, price, amount, fee,
    )
    return {
        "ok": True,
        "order_id": order_id,
        "symbol": symbol,
        "quantity": qty,
        "price": round(price, 3),
        "amount": amount,
        "fee": fee,
        "cash_balance": round(cash - amount - fee, 2),
    }


def execute_user_sell(
    user_id: str,
    symbol: str,
    portion: float = 1.0,
    price: float = 0.0,
    quantity: int = 0,
    reason: str = "",
    flow_id: str = "",
) -> dict:
    """在指定用户账户执行卖出并记录该笔盈亏; portion∈(0,1] 为卖出比例.

    price<=0 时自取行情 (扣滑点). 校验持仓存在与 T+1.
    """
    user_id = str(user_id).strip()
    account = db_ops.get_user_account(user_id)
    if account is None:
        return {"ok": False, "reason": "账户不存在"}
    holding = db_ops.get_holding(symbol, user_id=user_id)
    if not holding or int(holding.get("quantity", 0) or 0) <= 0:
        return {"ok": False, "reason": f"无 {symbol} 持仓"}
    if _t1_active(holding):
        return {"ok": False, "reason": f"T+1: {holding.get('cannot_sell_until')} 前禁止卖出"}

    total = int(holding["quantity"])
    if quantity > 0:
        qty = min(round_lot(int(quantity)), total)
        if quantity >= total:
            qty = total  # 清仓不受整手约束
    elif portion >= 1.0:
        qty = total
    else:
        qty = min(round_lot(int(total * portion)), total)
    if qty <= 0:
        return {"ok": False, "reason": "可卖数量为 0"}

    if price <= 0:
        price, _ = _quote_price(symbol)
        if price <= 0:
            return {"ok": False, "reason": f"无法获取 {symbol} 行情"}
        price = price * (1 - SLIPPAGE_BPS / 10000)  # 市价卖出滑点

    amount = round(price * qty, 2)
    fee = calc_fee(amount, OrderSide.SELL)
    cost = float(holding.get("cost_price", 0.0) or 0.0)
    realized_pnl = round((price - cost) * qty - fee, 2)
    pnl_pct = round((price - cost) / cost, 4) if cost > 0 else 0.0

    # ---- 成交: 回款 + 持仓更新/清仓 + 流水(含单笔盈亏) ----
    db_ops.adjust_user_cash(user_id, amount - fee)
    remaining = total - qty
    if remaining <= 0:
        db_ops.delete_holding(symbol, user_id=user_id)
    else:
        db_ops.upsert_holding({
            "symbol": symbol,
            "quantity": remaining,
            "available_quantity": remaining,
        }, user_id=user_id)

    order_id = f"USR-{uuid.uuid4().hex[:12]}"
    db_ops.add_trade_log({
        "user_id": user_id,
        "order_id": order_id,
        "symbol": symbol,
        "name": holding.get("name", ""),
        "side": "sell",
        "price": round(price, 3),
        "quantity": qty,
        "amount": amount,
        "fee": fee,
        "status": "filled",
        "reason": reason,
        "broker": "simulated",
        "cost_price": cost,
        "realized_pnl": realized_pnl,
        "pnl_pct": pnl_pct,
    })
    db_ops.log_decision(
        agent="user_account", decision="sell_filled", symbol=symbol,
        flow_id=flow_id, reason=reason[:500],
        detail={
            "user_id": user_id, "quantity": qty, "price": round(price, 3),
            "realized_pnl": realized_pnl, "pnl_pct": pnl_pct,
        },
    )
    logger.info(
        "USER SELL %s %s ×%d @%.3f pnl=%.2f (%.2f%%)",
        user_id, symbol, qty, price, realized_pnl, pnl_pct * 100,
    )
    return {
        "ok": True,
        "order_id": order_id,
        "symbol": symbol,
        "quantity": qty,
        "price": round(price, 3),
        "amount": amount,
        "fee": fee,
        "cost_price": cost,
        "realized_pnl": realized_pnl,
        "pnl_pct": pnl_pct,
        "cleared": remaining <= 0,
    }


# ---------------------------------------------------------------------------
# 引擎扇出 — 系统账户成交后同步到全部活跃用户账户
# ---------------------------------------------------------------------------

def fanout_buy(
    symbol: str,
    price: float,
    name: str = "",
    reason: str = "",
    plan: dict | None = None,
    stop_loss: float = 0.0,
    take_profit: float = 0.0,
    flow_id: str = "",
) -> dict:
    """引擎建仓/加仓扇出: 每个活跃用户账户按自身资金独立执行买入."""
    results: dict[str, dict] = {}
    for acct in db_ops.get_user_accounts(active_only=True):
        uid = acct["user_id"]
        try:
            results[uid] = execute_user_buy(
                uid, symbol, price=price, name=name, reason=reason,
                plan=plan, stop_loss=stop_loss, take_profit=take_profit,
                flow_id=flow_id,
            )
        except Exception as exc:  # 单用户失败不影响其他账户
            logger.exception("fanout_buy failed for user %s/%s", uid, symbol)
            results[uid] = {"ok": False, "reason": f"执行异常: {exc}"}
    filled = sum(1 for r in results.values() if r.get("ok"))
    summary = {"accounts": len(results), "filled": filled, "skipped": len(results) - filled}
    if results:
        db_ops.log_decision(
            agent="user_fanout", decision="fanout_buy", symbol=symbol,
            flow_id=flow_id, reason=f"扇出买入 {filled}/{len(results)} 账户成交",
            detail={"summary": summary,
                    "skipped_reasons": {u: r.get("reason", "")
                                        for u, r in results.items() if not r.get("ok")}},
        )
    return summary


def fanout_sell(
    symbol: str,
    portion: float = 1.0,
    price: float = 0.0,
    reason: str = "",
    flow_id: str = "",
) -> dict:
    """引擎减仓/清仓扇出: 每个持有该标的的用户账户按同比例卖出."""
    results: dict[str, dict] = {}
    for acct in db_ops.get_user_accounts(active_only=True):
        uid = acct["user_id"]
        holding = db_ops.get_holding(symbol, user_id=uid)
        if not holding or int(holding.get("quantity", 0) or 0) <= 0:
            continue
        try:
            results[uid] = execute_user_sell(
                uid, symbol, portion=portion, price=price,
                reason=reason, flow_id=flow_id,
            )
        except Exception as exc:
            logger.exception("fanout_sell failed for user %s/%s", uid, symbol)
            results[uid] = {"ok": False, "reason": f"执行异常: {exc}"}
    filled = sum(1 for r in results.values() if r.get("ok"))
    summary = {"holders": len(results), "filled": filled, "skipped": len(results) - filled}
    if results:
        db_ops.log_decision(
            agent="user_fanout", decision="fanout_sell", symbol=symbol,
            flow_id=flow_id, reason=f"扇出卖出 {filled}/{len(results)} 账户成交",
            detail={"summary": summary,
                    "skipped_reasons": {u: r.get("reason", "")
                                        for u, r in results.items() if not r.get("ok")}},
        )
    return summary
