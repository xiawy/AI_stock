"""BrokerAdapter trading layer (设计文档 §12).

抽象接口 + 两套实现:
- ``SimulatedBroker``     — 模拟盘: T+1 / 涨跌停 / 手续费 / 滑点 / 整手;
  持仓与现金从 trade_log 重建 (幂等, 重启不丢状态)
- ``HuaxingBrokerAdapter`` — 华兴证券实盘预留: 权限校验已内置, 具体行情/
  委托接口待官方 QMT/PTrade 文档接入

⚠️ 本系统为 AI 辅助决策系统, 不构成投资建议; 实盘使用风险自担.
"""

from __future__ import annotations

import logging
import math
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from . import db_ops
from .config import (
    COMMISSION_MIN,
    COMMISSION_RATE,
    LOT_SIZE,
    SLIPPAGE_BPS,
    STAMP_TAX_RATE,
    TRANSFER_FEE_RATE,
)

logger = logging.getLogger(__name__)

# A 股市场时区: T+1 / 状态重建的日界口径 (与 db_ops/_MARKET_TZ 对齐,
# 避免主机时区非东八区时本地 datetime.now() 切错日界)
_MARKET_TZ = timezone(timedelta(hours=8))


def _trade_date_market(ts: str) -> str:
    """trade_time (UTC 存储) 转 A 股市场时区日期, 回放 T+1 口径一致."""
    try:
        dt = datetime.fromisoformat(ts or "")
    except ValueError:
        return (ts or "")[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_MARKET_TZ).strftime("%Y-%m-%d")


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(Enum):
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass
class OrderRequest:
    symbol: str
    side: OrderSide
    quantity: int
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    reason: str = ""            # 决策理由 (审计)
    flow_id: str = ""
    extra: Optional[Dict[str, Any]] = None


@dataclass
class OrderResult:
    order_id: str
    status: OrderStatus
    filled_quantity: int = 0
    avg_fill_price: float = 0.0
    fee: float = 0.0
    amount: float = 0.0
    message: str = ""


@dataclass
class Position:
    symbol: str
    quantity: int               # 可用数量 (考虑 T+1)
    total_quantity: int         # 总持仓 (含当日买入冻结)
    avg_cost: float
    last_price: float
    market_value: float
    unrealized_pnl: float
    buy_date: str = ""


@dataclass
class AccountInfo:
    total_assets: float
    available_cash: float
    frozen_cash: float
    total_market_value: float


class OrderRejectException(Exception):
    """订单被 A 股约束/权限规则拒绝."""


def calc_fee(amount: float, side: OrderSide) -> float:
    """A 股费用: 佣金(万2.5, 最低5元) + 印花税(卖出 万5) + 过户费(十万分之1)."""
    commission = max(amount * COMMISSION_RATE, COMMISSION_MIN)
    stamp = amount * STAMP_TAX_RATE if side == OrderSide.SELL else 0.0
    transfer = amount * TRANSFER_FEE_RATE
    return round(commission + stamp + transfer, 2)


def round_lot(quantity: int) -> int:
    """A 股整手约束: 向下取整到 100 股."""
    return max((quantity // LOT_SIZE) * LOT_SIZE, 0)


# ---------------------------------------------------------------------------
# BrokerAdapter abstract (§12.1)
# ---------------------------------------------------------------------------

class BrokerAdapter(ABC):
    """交易适配层抽象接口."""

    broker_name = "abstract"

    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def get_account_info(self) -> AccountInfo: ...

    @abstractmethod
    def get_positions(self) -> List[Position]: ...

    @abstractmethod
    def place_order(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    def query_order(self, order_id: str) -> OrderResult: ...

    # 可选能力
    def subscribe_quote(self, symbols: List[str], callback) -> None:  # pragma: no cover
        raise NotImplementedError

    def register_order_callback(self, callback) -> None:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# SimulatedBroker (§12.2)
# ---------------------------------------------------------------------------

class SimulatedBroker(BrokerAdapter):
    """模拟盘: 强制 A 股约束, 状态从 trade_log 重建 (重启可恢复).

    - T+1: 当日买入标的 sell 直接拒绝
    - 涨跌停: 涨停不可买入, 跌停不可卖出 (按昨收 ±10/20/30%)
    - 市价单立即成交 (含滑点); 限价单价格不触及则当日不成交 (模拟挂单)
    - 手续费/印花税/过户费 按真实费率
    - 行情通过注入的 quote_fn 获取 (默认 DataService)
    """

    broker_name = "simulated"

    def __init__(self, quote_fn: Optional[Callable[[str], Optional[dict]]] = None):
        self._quote_fn = quote_fn or self._default_quote_fn
        self._cash: Optional[float] = None
        # symbol -> {"qty": int, "available": int, "cost": float, "buy_date": str}
        self._positions: Dict[str, dict] = {}
        self._today = None
        # 多队列消费线程并发下单保护 (现金/持仓内存态竞态)
        self._lock = threading.RLock()

    @staticmethod
    def _market_today() -> str:
        """市场日期 (北京时间): T+1 与状态重建的唯一日界口径."""
        return datetime.now(_MARKET_TZ).strftime("%Y-%m-%d")

    # -- 行情 ---------------------------------------------------------------
    @staticmethod
    def _default_quote_fn(symbol: str) -> Optional[dict]:
        try:
            from .data_service import get_data_service

            return get_data_service().get_realtime_quote(symbol)
        except Exception as exc:
            logger.warning("quote fetch failed for %s: %s", symbol, exc)
            return None

    def _quote(self, symbol: str) -> dict:
        quote = self._quote_fn(symbol)
        if not quote or quote.get("price", 0) <= 0:
            raise OrderRejectException(f"无法获取 {symbol} 实时行情, 拒绝下单")
        return quote

    # -- 状态重建 -----------------------------------------------------------
    def _ensure_state(self) -> None:
        """从 trade_log 重建现金与持仓 (幂等; 每个交易日首次访问时重建).

        available 按 T+1 回放: 买入日早于今天的份额计入可用, 当日买入冻结;
        卖出按时间序冲减可用. 修复旧版回放后 available 恒为 0 导致当日加仓后
        旧仓被整体冻结无法卖出的问题.
        """
        today = self._market_today()
        if self._cash is not None and self._today == today:
            return
        with self._lock:
            if self._cash is not None and self._today == today:
                return  # 其他线程已完成本日重建
            initial = float(db_ops.get_config_value("initial_cash", "1000000"))
            cash = initial
            positions: Dict[str, dict] = {}
            trades = db_ops.get_trades(limit=0)  # 全量回放, 不截断 (防状态重建不完整)
            for t in reversed(trades):  # 时间正序回放
                if t["status"] != "filled":
                    continue
                sym = t["symbol"]
                trade_date = _trade_date_market(t.get("trade_time"))
                pos = positions.setdefault(
                    sym, {"qty": 0, "available": 0, "cost": 0.0, "buy_date": ""},
                )
                if t["side"] == "buy":
                    total_cost = pos["cost"] * pos["qty"] + t["amount"] + t["fee"]
                    pos["qty"] += t["quantity"]
                    pos["cost"] = total_cost / pos["qty"] if pos["qty"] else 0.0
                    pos["buy_date"] = trade_date
                    if trade_date and trade_date < today:
                        pos["available"] += t["quantity"]  # 隔日买入已解冻
                    cash -= t["amount"] + t["fee"]
                elif t["side"] == "sell":
                    pos["qty"] = max(pos["qty"] - t["quantity"], 0)
                    pos["available"] = max(pos["available"] - t["quantity"], 0)
                    cash += t["amount"] - t["fee"]
            self._cash = cash
            self._positions = positions
            self._today = today
            logger.info(
                "SimulatedBroker state rebuilt: cash=%.2f, %d positions",
                cash, len(positions),
            )

    def _refresh_available(self, symbol: str) -> None:
        """T+1: 过了最后一笔买入日, 冻结份额转为可用."""
        pos = self._positions.get(symbol)
        if not pos or not pos["buy_date"]:
            return
        today = self._market_today()
        if pos["buy_date"] < today:
            # 最后一笔买入已隔日 → 剩余持仓全部可卖 (含当日部分减仓后的余量)
            pos["available"] = pos["qty"]

    # -- BrokerAdapter API ----------------------------------------------------
    def connect(self) -> bool:
        self._ensure_state()
        return True

    def disconnect(self) -> None:
        return

    def get_account_info(self) -> AccountInfo:
        self._ensure_state()
        market_value = 0.0
        for sym, pos in self._positions.items():
            if pos["qty"] <= 0:
                continue
            quote = self._quote_fn(sym) or {}
            price = quote.get("price") or pos["cost"]
            market_value += price * pos["qty"]
        return AccountInfo(
            total_assets=round(self._cash + market_value, 2),
            available_cash=round(self._cash, 2),
            frozen_cash=0.0,
            total_market_value=round(market_value, 2),
        )

    def get_positions(self) -> List[Position]:
        self._ensure_state()
        out = []
        for sym, pos in self._positions.items():
            if pos["qty"] <= 0:
                continue
            self._refresh_available(sym)
            quote = self._quote_fn(sym) or {}
            price = quote.get("price") or pos["cost"]
            market_value = price * pos["qty"]
            out.append(Position(
                symbol=sym,
                quantity=pos["available"],
                total_quantity=pos["qty"],
                avg_cost=round(pos["cost"], 3),
                last_price=price,
                market_value=round(market_value, 2),
                unrealized_pnl=round((price - pos["cost"]) * pos["qty"], 2),
                buy_date=pos["buy_date"],
            ))
        return out

    def place_order(self, request: OrderRequest) -> OrderResult:
        # buy/hold/risk 多队列消费线程可能并发下单, 全程持锁防现金/持仓竞态;
        # RLock 允许锁内调用的 _ensure_state 再次加锁重建状态.
        with self._lock:
            return self._place_order_impl(request)

    def _place_order_impl(self, request: OrderRequest) -> OrderResult:
        self._ensure_state()
        order_id = f"SIM-{uuid.uuid4().hex[:12]}"
        symbol = str(request.symbol).strip().lower()
        for prefix in ("sh", "sz", "bj"):
            symbol = symbol.removeprefix(prefix)

        quote = self._quote(symbol)
        price = quote.get("price", 0.0)
        limit_up = quote.get("limit_up", 0.0)
        limit_down = quote.get("limit_down", 0.0)

        # ---- A 股约束 ----
        qty = round_lot(request.quantity)
        if qty <= 0:
            return self._reject(order_id, request, "委托数量不足一整手 (100股)")

        if request.side == OrderSide.BUY:
            if price >= limit_up > 0:
                return self._reject(order_id, request, f"涨停价 {limit_up} 不可买入")
        else:
            if price <= limit_down > 0:
                return self._reject(order_id, request, f"跌停价 {limit_down} 不可卖出")
            self._refresh_available(symbol)
            pos = self._positions.get(symbol)
            if not pos or pos["available"] < qty:
                avail = pos["available"] if pos else 0
                if pos and pos["qty"] >= qty and pos["buy_date"] == self._market_today():
                    return self._reject(order_id, request, "T+1: 当日买入不可卖出")
                return self._reject(order_id, request, f"可卖数量不足 (可用 {avail} < {qty})")

        # ---- 成交价模拟 ----
        if request.order_type == OrderType.LIMIT and request.limit_price:
            # 限价单: 价格不触及则模拟挂单未成交
            if request.side == OrderSide.BUY and request.limit_price < price:
                return OrderResult(
                    order_id=order_id, status=OrderStatus.PENDING,
                    message=f"限价 {request.limit_price} 低于现价 {price}, 挂单未成交",
                )
            if request.side == OrderSide.SELL and request.limit_price > price:
                return OrderResult(
                    order_id=order_id, status=OrderStatus.PENDING,
                    message=f"限价 {request.limit_price} 高于现价 {price}, 挂单未成交",
                )
            fill_price = request.limit_price
        else:
            slip = price * SLIPPAGE_BPS / 10000
            fill_price = price + slip if request.side == OrderSide.BUY else price - slip

        amount = round(fill_price * qty, 2)
        fee = calc_fee(amount, request.side)

        if request.side == OrderSide.BUY and amount + fee > self._cash:
            return self._reject(order_id, request, f"可用资金不足 (需 {amount + fee:.2f}, 有 {self._cash:.2f})")

        # ---- 成交: 更新内存 + 落库 ----
        realized_pnl = 0.0
        pnl_pct = 0.0
        cost_price = 0.0
        if request.side == OrderSide.BUY:
            pos = self._positions.setdefault(
                symbol, {"qty": 0, "available": 0, "cost": 0.0, "buy_date": ""},
            )
            total_cost = pos["cost"] * pos["qty"] + amount + fee
            pos["qty"] += qty
            pos["cost"] = total_cost / pos["qty"]
            pos["buy_date"] = self._market_today()  # T+1 冻结
            self._cash -= amount + fee
        else:
            pos = self._positions[symbol]
            cost_price = float(pos.get("cost", 0.0) or 0.0)
            # 单笔已实现盈亏须在减仓前用原持仓成本计算 (含卖出费用)
            realized_pnl = round((fill_price - cost_price) * qty - fee, 2)
            pnl_pct = round((fill_price - cost_price) / cost_price, 4) if cost_price > 0 else 0.0
            pos["qty"] -= qty
            pos["available"] -= qty
            self._cash += amount - fee
            if pos["qty"] <= 0:
                self._positions.pop(symbol, None)

        trade_entry = {
            "order_id": order_id,
            "symbol": symbol,
            "name": quote.get("name", ""),
            "side": request.side.value,
            "price": round(fill_price, 3),
            "quantity": qty,
            "amount": amount,
            "fee": fee,
            "status": "filled",
            "reason": request.reason,
            "broker": self.broker_name,
        }
        if request.side == OrderSide.SELL:
            trade_entry.update({
                "cost_price": cost_price,
                "realized_pnl": realized_pnl,
                "pnl_pct": pnl_pct,
            })
        db_ops.add_trade_log(trade_entry)
        logger.info(
            "SIM FILLED %s %s ×%d @%.3f amount=%.2f fee=%.2f",
            request.side.value, symbol, qty, fill_price, amount, fee,
        )
        return OrderResult(
            order_id=order_id,
            status=OrderStatus.FILLED,
            filled_quantity=qty,
            avg_fill_price=round(fill_price, 3),
            fee=fee,
            amount=amount,
            message=request.reason,
        )

    def cancel_order(self, order_id: str) -> bool:
        # 模拟盘市价单即时成交, 限价挂单简化为直接取消成功
        logger.info("SIM cancel %s (accepted)", order_id)
        return True

    def query_order(self, order_id: str) -> OrderResult:
        # 状态不明确时必须确认 — 模拟盘直接返回 unknown, 由调用方查 trade_log
        return OrderResult(
            order_id=order_id, status=OrderStatus.UNKNOWN,
            message="simulated broker: query trade_log for final state",
        )

    def _reject(self, order_id: str, request: OrderRequest, message: str) -> OrderResult:
        db_ops.add_trade_log({
            "order_id": order_id,
            "symbol": request.symbol,
            "side": request.side.value,
            "status": "rejected",
            "reason": message,
            "broker": self.broker_name,
        })
        logger.warning("SIM REJECTED %s %s: %s", request.side.value, request.symbol, message)
        return OrderResult(
            order_id=order_id, status=OrderStatus.REJECTED, message=message,
        )


# ---------------------------------------------------------------------------
# HuaxingBrokerAdapter (§12.3 实盘预留)
# ---------------------------------------------------------------------------

class HuaxingBrokerAdapter(BrokerAdapter):
    """华兴证券实盘适配器 — 预留实现.

    华兴为境内持牌券商, 量化接口以官方 QMT / PTrade 等 Python API 文档
    为准. 本类已内置:
    - place_order 前的权限检查 (全局开关 / 单日笔数 / 单笔金额 / 白名单)
    - 订单状态不明确时强制 query_order 确认 (防重复下单)
    对接流程 (§12.3.3): 申请权限 → 开发适配 → 模拟验证 → 风控联调 →
    灰度实盘(极小仓位观察5交易日) → 正式启用.
    """

    broker_name = "huaxing"

    def __init__(self, api_client=None):
        self._client = api_client  # 官方 SDK client (待接入)
        self._connected = False

    def connect(self) -> bool:
        if self._client is None:
            logger.error(
                "HuaxingBrokerAdapter 未配置官方 API client — "
                "请申请华兴量化权限后按官方文档接入"
            )
            return False
        try:
            self._connected = bool(self._client.login())
            return self._connected
        except Exception as exc:
            logger.error("华兴证券连接失败: %s", exc)
            return False

    def disconnect(self) -> None:
        if self._connected and self._client is not None:
            try:
                self._client.logout()
            except Exception:
                pass
        self._connected = False

    def _ensure_live_permission(self, request: OrderRequest) -> None:
        """实盘权限检查 (§12.3.2): 开关/单日笔数/单笔金额/白名单."""
        if db_ops.get_config_value("global_trade_enable", "0") != "1":
            raise OrderRejectException("实盘全局开关关闭 (global_trade_enable=0)")
        max_orders = int(db_ops.get_config_value("max_daily_orders", "50"))
        if db_ops.count_orders_today() >= max_orders:
            raise OrderRejectException(f"超过单日最大委托笔数 {max_orders}")
        max_value = float(db_ops.get_config_value("max_order_value", "200000"))
        quote = self._quote(request.symbol)
        est_amount = quote.get("price", 0) * request.quantity
        if est_amount > max_value:
            raise OrderRejectException(f"单笔金额 {est_amount:.0f} 超上限 {max_value}")
        whitelist = [
            s.strip() for s in db_ops.get_config_value("allowed_symbols", "").split(",")
            if s.strip()
        ]
        if whitelist and request.symbol not in whitelist:
            raise OrderRejectException(f"{request.symbol} 不在实盘白名单")

    @staticmethod
    def _quote(symbol: str) -> dict:
        from .data_service import get_data_service

        quote = get_data_service().get_realtime_quote(symbol)
        if not quote:
            raise OrderRejectException(f"无法获取 {symbol} 行情")
        return quote

    def get_account_info(self) -> AccountInfo:
        if not self._connected:
            raise RuntimeError("华兴证券未连接")
        info = self._client.get_account()
        return AccountInfo(
            total_assets=info.get("total_assets", 0.0),
            available_cash=info.get("available_cash", 0.0),
            frozen_cash=info.get("frozen_cash", 0.0),
            total_market_value=info.get("market_value", 0.0),
        )

    def get_positions(self) -> List[Position]:
        if not self._connected:
            raise RuntimeError("华兴证券未连接")
        rows = self._client.get_positions()
        return [
            Position(
                symbol=r["symbol"],
                quantity=r.get("available", r.get("quantity", 0)),
                total_quantity=r.get("quantity", 0),
                avg_cost=r.get("avg_cost", 0.0),
                last_price=r.get("last_price", 0.0),
                market_value=r.get("market_value", 0.0),
                unrealized_pnl=r.get("unrealized_pnl", 0.0),
            )
            for r in rows
        ]

    def place_order(self, request: OrderRequest) -> OrderResult:
        if not self._connected:
            raise RuntimeError("华兴证券未连接")
        self._ensure_live_permission(request)  # 权限不通过直接抛 OrderRejectException
        order_id = ""
        try:
            order_id = self._client.place_order(
                symbol=request.symbol,
                side=request.side.value,
                quantity=request.quantity,
                order_type=request.order_type.value,
                price=request.limit_price,
            )
        except TimeoutError:
            # 网络超时: 状态不明确 → 强制 query_order 确认, 避免重复下单
            confirmed = self.query_order(order_id)
            if confirmed.status == OrderStatus.UNKNOWN:
                raise OrderRejectException("委托状态不明确, 已转人工确认") from None
            return confirmed
        # 拉取一次状态
        return self.query_order(order_id)

    def cancel_order(self, order_id: str) -> bool:
        if not self._connected:
            return False
        try:
            return bool(self._client.cancel_order(order_id))
        except Exception as exc:
            logger.error("华兴撤单失败 %s: %s", order_id, exc)
            return False

    def query_order(self, order_id: str) -> OrderResult:
        if not self._connected or not order_id:
            return OrderResult(order_id=order_id, status=OrderStatus.UNKNOWN)
        try:
            d = self._client.query_order(order_id)
            status_map = {
                "pending": OrderStatus.PENDING,
                "partially_filled": OrderStatus.PARTIALLY_FILLED,
                "filled": OrderStatus.FILLED,
                "cancelled": OrderStatus.CANCELLED,
                "rejected": OrderStatus.REJECTED,
            }
            return OrderResult(
                order_id=order_id,
                status=status_map.get(d.get("status"), OrderStatus.UNKNOWN),
                filled_quantity=int(d.get("filled_quantity", 0)),
                avg_fill_price=float(d.get("avg_price", 0.0)),
                message=d.get("message", ""),
            )
        except Exception as exc:
            logger.error("华兴查单失败 %s: %s", order_id, exc)
            return OrderResult(order_id=order_id, status=OrderStatus.UNKNOWN)


_broker: Optional[BrokerAdapter] = None


def get_broker() -> BrokerAdapter:
    """默认返回模拟盘; 实盘需显式装配 HuaxingBrokerAdapter."""
    global _broker
    if _broker is None:
        _broker = SimulatedBroker()
    return _broker


def set_broker(broker: BrokerAdapter) -> None:
    global _broker
    _broker = broker
