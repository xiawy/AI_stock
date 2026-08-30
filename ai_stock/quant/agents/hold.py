"""持仓维护 Agent 集群 (设计文档 §7.3, 队列: hold).

流程 (Orchestrator FSM 驱动, 每只持仓一个 flow):
    hold_scan(入口) → 走势跟踪 → 加仓决策 → 减仓/清仓决策 → 执行与规划更新

原则:
- 止损/止盈全部多 K 线或收盘确认, 瞬时价格不触发指令 (§7.3.1)
- 时间止损严格按 A 股交易日计数 (calendar_utils)
- 硬性规则: 亏损超 5% 清仓; 5 个交易日无大行情清仓 (规则引擎硬规则)
- 移动止盈上移需收盘价确认 (§7.3.2)
- 新增重大利空可跳过技术指标直接清仓建议 (§7.3.3, news_store 检索)
- 全部调仓指令强制 pre_trade_check (§7.3.4)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel

from .. import db_ops
from ..broker import (
    OrderRequest,
    OrderSide,
    OrderType,
    get_broker,
    round_lot,
)
from ..calendar_utils import count_trading_days
from ..config import (
    BUY_POSITION_RATIO,
    SINGLE_POSITION_MAX_PCT,
    STOP_LOSS_PCT,
    TIME_STOP_MIN_GAIN,
    TIME_STOP_TRADING_DAYS,
    TRAILING_STOP_FLOOR,
    TRAILING_STOP_TRIGGER,
)
from ..llm_helper import structured_invoke
from ..news_store import get_news_store
from ..orchestrator import get_orchestrator
from ..risk_control import account_snapshot, pre_trade_check
from ..rules_engine import get_rule_engine
from .base import BaseAgent

logger = logging.getLogger(__name__)


def _parse_dt(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 持仓扫描入口
# ---------------------------------------------------------------------------

def handle_hold_scan(task: dict) -> dict:
    """hold 队列 hold_scan handler: 遍历持仓, 逐只启动维护流程 (10min 幂等)."""
    payload = task.get("payload", {})
    window = payload.get("window") or datetime.now().strftime("%Y%m%d%H%M")
    orchestrator = get_orchestrator()

    started: list[str] = []
    for holding in db_ops.get_holdings():
        symbol = holding.get("symbol", "")
        if not symbol:
            continue
        orchestrator.start_flow(
            "hold",
            flow_id=f"hold_{symbol}_{window}",
            data={
                "symbol": symbol,
                "name": holding.get("name", ""),
                "quantity": holding.get("quantity", 0),
                "cost_price": holding.get("cost_price", 0.0),
                "buy_time": holding.get("buy_time", ""),
                "plan": holding.get("plan") or {},
                "predicted_path": holding.get("predicted_path") or {},
                "stop_loss": holding.get("stop_loss", 0.0),
                "take_profit": holding.get("take_profit", 0.0),
            },
        )
        started.append(symbol)
    return {"started_flows": started}


# ---------------------------------------------------------------------------
# 7.3.1 走势跟踪 Agent
# ---------------------------------------------------------------------------

class TrendTrackingAgent(BaseAgent):
    """持仓走势跟踪: 与买入时预测对比 + 止损/时间止损信号 (多 K 线确认)."""

    agent_name = "trend_tracking"

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")
        symbol = context.get("symbol", "")
        cost = float(context.get("cost_price", 0) or 0)

        indicators = self.data.get_stock_indicators(symbol)
        quote = self.data.get_realtime_quote(symbol)
        if not indicators or not quote or cost <= 0:
            result = {
                "decision": "no_data", "abort": True,
                "abort_reason": f"{symbol} 行情/指标数据不可用, 本周期跳过",
            }
            self.report(flow_type, flow_id, step, result)
            return result

        close_series = indicators.get("close_series") or []
        high_series = indicators.get("high_series") or []
        current_close = close_series[-1] if close_series else quote.get("price", cost)
        loss_ratio = max(0.0, (cost - current_close) / cost)
        gain_ratio = max(0.0, (current_close - cost) / cost)
        max_high = max(high_series) if high_series else current_close
        max_gain_ratio = max(0.0, (max_high - cost) / cost) if max_high else 0.0
        atr = indicators.get("atr") or 0.0
        atr_ratio = (atr / current_close) if current_close else 1.0

        # 持有交易日数 (严格交易日计数, §5.2; 买入日之后的交易日数,
        # 对应 §2.3 "买入后五个交易日无大行情直接清仓")
        buy_dt = _parse_dt(context.get("buy_time"))
        holding_trading_days = (
            count_trading_days(buy_dt.date() + timedelta(days=1), datetime.now().date())
            if buy_dt else 0
        )

        # 规则引擎上下文 (硬规则: stop_loss_5pct / time_stop_5days)
        rule_ctx = {
            "current_loss_ratio": round(loss_ratio, 4),
            "current_gain_ratio": round(gain_ratio, 4),
            "holding_trading_days": holding_trading_days,
            "max_gain_ratio": round(max_gain_ratio, 4),
            "atr_ratio": round(atr_ratio, 4),
            "position_value_pct": 0.0,  # 风控扫描单独覆盖
        }
        triggered_rules = get_rule_engine().evaluate(rule_ctx)
        rule_actions = [r["action"] for r in triggered_rules]

        # 止损信号: 连续两根 K 线收盘浮亏 ≥5% (§7.3.1, 不用盘中瞬时价)
        stop_loss_signal = "sell_all" in rule_actions or (
            len(close_series) >= 2
            and close_series[-1] <= cost * (1 - STOP_LOSS_PCT)
            and close_series[-2] <= cost * (1 - STOP_LOSS_PCT)
        )
        # 时间止损信号
        time_stop_signal = "sell_all" in rule_actions and holding_trading_days >= TIME_STOP_TRADING_DAYS
        if not time_stop_signal:
            time_stop_signal = (
                holding_trading_days >= TIME_STOP_TRADING_DAYS
                and max_gain_ratio < TIME_STOP_MIN_GAIN
                and atr_ratio < 0.04
            )

        # 走势对比: 与买入时预测路径对比 (LLM 预测文本 + 简单数据对比)
        predicted = (context.get("predicted_path") or {}).get("text", "")
        trend = "as_expected"
        if gain_ratio >= 0.08 or "sell_all" not in rule_actions and max_gain_ratio >= 0.10:
            trend = "above"       # 超预期
        elif loss_ratio >= STOP_LOSS_PCT * 0.6:
            trend = "below"       # 不及预期

        result = {
            "decision": "tracked",
            "symbol": symbol,
            "current_price": quote.get("price"),
            "current_close": current_close,
            "loss_ratio": round(loss_ratio, 4),
            "gain_ratio": round(gain_ratio, 4),
            "max_gain_ratio": round(max_gain_ratio, 4),
            "atr_ratio": round(atr_ratio, 4),
            "holding_trading_days": holding_trading_days,
            "trend": trend,
            "stop_loss_signal": stop_loss_signal,
            "time_stop_signal": time_stop_signal,
            "triggered_rules": triggered_rules,
            "predicted_path": predicted[:300],
        }
        self.log(
            "trend_tracked", symbol=symbol, task_id=task_id, flow_id=flow_id,
            reason=(
                f"trend={trend}, 浮亏={loss_ratio:.1%}, 持有{holding_trading_days}交易日, "
                f"止损信号={stop_loss_signal}, 时间止损={time_stop_signal}"
            ),
            detail=result,
        )
        self.report(flow_type, flow_id, step, result)
        return result


# ---------------------------------------------------------------------------
# 7.3.2 加仓决策 Agent
# ---------------------------------------------------------------------------

class AddPositionAgent(BaseAgent):
    """加仓条件判断: 新催化 / 走势超预期 / 突破关键位置; 移动止盈需收盘确认."""

    agent_name = "add_position"

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")
        symbol = context.get("symbol", "")
        cost = float(context.get("cost_price", 0) or 0)
        trend_result = context.get("trend_tracking") or {}
        indicators = self.data.get_stock_indicators(symbol)

        close_series = (indicators or {}).get("close_series") or []
        high_series = (indicators or {}).get("high_series") or []
        current_close = close_series[-1] if close_series else 0.0
        gain_ratio = trend_result.get("gain_ratio", 0.0)

        # 突破关键位置: 收盘突破近 10 日高点 (前高, 不含当日)
        breakout = bool(
            len(high_series) >= 11
            and current_close > max(high_series[:-1])
        )
        above_expectation = trend_result.get("trend") == "above"
        new_catalyst = self._check_new_catalyst(symbol, context)

        should_add = bool(breakout or above_expectation or new_catalyst) and \
            not trend_result.get("stop_loss_signal") and \
            not trend_result.get("time_stop_signal")

        # 移动止盈: 收盘价确认盈利超阈值 → 止损位上移 (§7.3.2)
        new_stop_loss = None
        if current_close and cost and gain_ratio >= TRAILING_STOP_TRIGGER:
            new_stop_loss = round(cost * (1 + TRAILING_STOP_FLOOR), 3)
            self.log(
                "trailing_stop_raised", symbol=symbol, task_id=task_id,
                flow_id=flow_id,
                reason=f"收盘确认盈利 {gain_ratio:.1%} ≥ {TRAILING_STOP_TRIGGER:.0%}, "
                       f"止损上移至 {new_stop_loss}",
            )

        # 加仓金额: 可用资金 × 20%, 且总仓位不超过单只上限
        snapshot = account_snapshot()
        total_assets = snapshot.get("total_assets", 0.0) or 1.0
        quote = self.data.get_realtime_quote(symbol)
        current_value = (quote or {}).get("price", 0.0) * int(context.get("quantity", 0))
        headroom_pct = SINGLE_POSITION_MAX_PCT - current_value / total_assets
        add_budget = 0.0
        if should_add:
            add_budget = snapshot.get("available_cash", 0.0) * BUY_POSITION_RATIO
            max_add = headroom_pct * total_assets * 0.9  # 留 10% 缓冲
            add_budget = min(add_budget, max(0.0, max_add))

        result = {
            "decision": "add" if should_add and add_budget > 0 else "hold",
            "should_add": should_add and add_budget > 0,
            "add_budget": round(add_budget, 2),
            "breakout": breakout,
            "above_expectation": above_expectation,
            "new_catalyst": new_catalyst,
            "new_stop_loss": new_stop_loss,
        }
        self.log(
            "add_position" if result["should_add"] else "no_add",
            symbol=symbol, task_id=task_id, flow_id=flow_id,
            reason=(
                f"突破={breakout}, 超预期={above_expectation}, 新催化={new_catalyst}, "
                f"加仓预算={add_budget:.0f}"
            ),
            detail=result,
        )
        self.report(flow_type, flow_id, step, result)
        return result

    def _check_new_catalyst(self, symbol: str, context: dict) -> bool:
        """新催化检测: 检索近 3 日该股新闻, 出现新增正向事件即视为新催化."""
        try:
            results = get_news_store().search(
                f"{symbol} 利好 中标 订单 涨价 政策", days=3, n_results=5,
                symbol=symbol,
            )
            return len(results) > 0
        except Exception as exc:
            logger.warning("catalyst search failed for %s: %s", symbol, exc)
            return False


# ---------------------------------------------------------------------------
# 7.3.3 减仓/清仓决策 Agent
# ---------------------------------------------------------------------------

class MajorBearCheck(BaseModel):
    """新增重大利空确认 (LLM)."""

    has_major_bear: bool = False
    reason: str = ""


class ReducePositionAgent(BaseAgent):
    """卖出决策: 硬性止损/时间止损优先; 新增重大利空可跳过技术指标直接清仓."""

    agent_name = "reduce_position"

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")
        symbol = context.get("symbol", "")
        trend_result = context.get("trend_tracking") or {}

        action = "hold"
        reason = ""

        # 1) 硬性规则 (§2.3): 亏损超 5% / 5 交易日无大行情 → 直接清仓
        if trend_result.get("stop_loss_signal"):
            action = "sell_all"
            reason = (
                f"硬性止损: 浮亏 {trend_result.get('loss_ratio', 0):.1%} "
                f"(连续两根 K 线确认)"
            )
        elif trend_result.get("time_stop_signal"):
            action = "sell_all"
            reason = (
                f"时间止损: 持有 {trend_result.get('holding_trading_days', 0)} 个交易日, "
                f"最大涨幅 {trend_result.get('max_gain_ratio', 0):.1%} 无大行情"
            )
        else:
            # 2) 软性条件: 走势不及预期 / 新增重大利空
            major_bear = self._check_major_bear(symbol, context)
            if major_bear is not None and major_bear.has_major_bear:
                action = "sell_all"
                reason = f"新增重大利空, 跳过技术指标直接清仓: {major_bear.reason}"
            elif trend_result.get("trend") == "below":
                action = "sell_partial"
                reason = "走势不及预期, 减仓 1/2 降低风险"

        result = {
            "decision": action,
            "action": action,
            "reason": reason,
            "portion": 1.0 if action == "sell_all" else (
                0.5 if action == "sell_partial" else 0.0
            ),
        }
        self.log(
            f"reduce_{action}", symbol=symbol, task_id=task_id, flow_id=flow_id,
            reason=reason, detail=result,
        )
        self.report(flow_type, flow_id, step, result)
        return result

    def _check_major_bear(self, symbol: str, context: dict) -> Optional[MajorBearCheck]:
        """检索新增重大利空 (§7.3.3: 可跳过技术指标直接清仓)."""
        try:
            news = get_news_store().search(
                f"{symbol} 利空 处罚 亏损 减持 暴雷", days=3, n_results=5,
                symbol=symbol,
            )
        except Exception as exc:
            logger.warning("major bear search failed for %s: %s", symbol, exc)
            return None
        if not news:
            return None
        if not self.llm.available:
            return MajorBearCheck(
                has_major_bear=False,
                reason=f"检索到 {len(news)} 条相关新闻但 LLM 不可用, 无法确认利空等级",
            )
        digest = "\n".join(
            f"- {n.get('title', '')}: {(n.get('content', '') or '')[:120]}" for n in news
        )
        prompt = (
            f"A 股 {symbol} 近 3 日检索到以下新闻, 请判断是否出现新增重大利空\n"
            f"(业绩暴雷/监管处罚/核心客户流失/大股东大额减持/行业政策转向):\n\n{digest}\n\n"
            "注意: 一般性波动评论、旧闻重提不算重大利空。"
            "输出 has_major_bear/reason。"
        )
        try:
            return structured_invoke(
                self.llm.pick(), MajorBearCheck, prompt, default=MajorBearCheck(),
            )
        except Exception as exc:
            logger.warning("Major bear LLM check failed for %s: %s", symbol, exc)
            return None


# ---------------------------------------------------------------------------
# 7.3.4 操作执行与规划更新 Agent
# ---------------------------------------------------------------------------

class ExecutionUpdateAgent(BaseAgent):
    """执行加减仓指令 (强制 pre_trade_check) + 更新持仓操作规划."""

    agent_name = "execution_update"

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")
        symbol = context.get("symbol", "")
        reduce_result = context.get("reduce_position_decision") or {}
        add_result = context.get("add_position_decision") or {}
        action = reduce_result.get("action", "hold")

        executed: list[dict] = []

        # 1) 卖出指令 (清仓/减仓)
        if action in ("sell_all", "sell_partial") and reduce_result.get("portion", 0) > 0:
            executed.append(
                self._execute_sell(symbol, context, reduce_result, task_id, flow_id)
            )

        # 2) 加仓指令 (止损信号触发时绝不加仓)
        if add_result.get("should_add") and action == "hold":
            executed.append(
                self._execute_add(symbol, context, add_result, task_id, flow_id)
            )

        # 3) 更新操作规划 (新止损位 / 加仓条件状态)
        self._update_plan(symbol, context, add_result, reduce_result, executed)

        result = {
            "decision": "executed" if executed else "no_action",
            "executed": executed,
            "reduce_action": action,
        }
        self.log(
            "execution_done", symbol=symbol, task_id=task_id, flow_id=flow_id,
            reason=f"减仓决策={action}, 执行 {len(executed)} 笔指令",
            detail=result,
        )
        self.report(flow_type, flow_id, step, result)
        return result

    # -- 卖出执行 ------------------------------------------------------------

    def _execute_sell(
        self, symbol: str, context: dict, reduce_result: dict,
        task_id: str, flow_id: str,
    ) -> dict:
        portion = float(reduce_result.get("portion", 0))
        holding = db_ops.get_holding(symbol) or context
        total_qty = int(holding.get("quantity", 0) or context.get("quantity", 0))
        # 可卖数量: T+1 解冻刷新 (broker 持有权威数据)
        broker = get_broker()
        positions = {p.symbol: p for p in broker.get_positions()}
        pos = positions.get(symbol)
        available = pos.quantity if pos else int(
            holding.get("available_quantity", 0) or 0
        )
        sell_qty = round_lot(int(total_qty * portion)) if portion < 1.0 else total_qty
        sell_qty = min(sell_qty, available)
        if sell_qty <= 0:
            return {
                "op": "sell", "status": "skipped",
                "reason": f"可卖数量为 0 (T+1 冻结或已清仓)",
            }

        order = OrderRequest(
            symbol=symbol, side=OrderSide.SELL, quantity=sell_qty,
            order_type=OrderType.MARKET,
            reason=reduce_result.get("reason", "")[:200], flow_id=flow_id,
        )
        check = pre_trade_check(order)
        if not check.ok:
            self.log(
                "sell_risk_rejected", symbol=symbol, task_id=task_id,
                flow_id=flow_id, reason=f"卖出被风控拒绝: {check.reason}",
            )
            return {"op": "sell", "status": "risk_rejected", "reason": check.reason}

        result_order = broker.place_order(order)
        outcome = {
            "op": "sell",
            "status": result_order.status.value,
            "quantity": result_order.filled_quantity,
            "price": result_order.avg_fill_price,
            "amount": result_order.amount,
            "fee": result_order.fee,
        }
        if result_order.status.value == "filled":
            # 更新/删除持仓池记录
            remaining = total_qty - result_order.filled_quantity
            if remaining <= 0:
                db_ops.delete_holding(symbol)
                outcome["cleared"] = True
                self.log(
                    "position_cleared", symbol=symbol, task_id=task_id,
                    flow_id=flow_id, reason=reduce_result.get("reason", ""),
                )
            else:
                db_ops.upsert_holding({
                    "symbol": symbol,
                    "quantity": remaining,
                    "available_quantity": max(
                        0, min(remaining, available - result_order.filled_quantity),
                    ),
                })
            # 多用户扇出: 同一卖出决策按同比例在每个用户账户执行 (各自校验 T+1)
            from ..user_accounts import fanout_sell
            outcome["user_fanout"] = fanout_sell(
                symbol,
                portion=1.0 if outcome.get("cleared") else portion,
                price=result_order.avg_fill_price,
                reason=reduce_result.get("reason", "")[:200],
                flow_id=flow_id,
            )
        return outcome

    # -- 加仓执行 ------------------------------------------------------------

    def _execute_add(
        self, symbol: str, context: dict, add_result: dict,
        task_id: str, flow_id: str,
    ) -> dict:
        budget = float(add_result.get("add_budget", 0) or 0)
        quote = self.data.get_realtime_quote(symbol)
        if not quote or budget <= 0:
            return {"op": "add", "status": "skipped", "reason": "预算不足或行情不可用"}
        est_price = quote["price"] * 1.002
        qty = round_lot(int(budget / est_price / 100) * 100)
        if qty <= 0:
            return {"op": "add", "status": "skipped", "reason": "预算不足一手"}

        order = OrderRequest(
            symbol=symbol, side=OrderSide.BUY, quantity=qty,
            order_type=OrderType.MARKET,
            reason=f"加仓: {add_result.get('reason', '突破/超预期')}"[:200],
            flow_id=flow_id,
        )
        check = pre_trade_check(order)
        if not check.ok:
            self.log(
                "add_risk_rejected", symbol=symbol, task_id=task_id,
                flow_id=flow_id, reason=f"加仓被风控拒绝: {check.reason}",
            )
            return {"op": "add", "status": "risk_rejected", "reason": check.reason}

        result_order = get_broker().place_order(order)
        outcome = {
            "op": "add",
            "status": result_order.status.value,
            "quantity": result_order.filled_quantity,
            "price": result_order.avg_fill_price,
            "amount": result_order.amount,
            "fee": result_order.fee,
        }
        if result_order.status.value == "filled":
            # 多用户扇出: 加仓同步到每个用户账户 (各自按 20% 资金预算执行)
            from ..user_accounts import fanout_buy
            outcome["user_fanout"] = fanout_buy(
                symbol,
                price=result_order.avg_fill_price,
                reason=f"加仓: {add_result.get('reason', '')[:150]}",
                flow_id=flow_id,
            )
        return outcome

    # -- 规划更新 ------------------------------------------------------------

    def _update_plan(
        self, symbol: str, context: dict, add_result: dict,
        reduce_result: dict, executed: list[dict],
    ) -> None:
        holding = db_ops.get_holding(symbol)
        if holding is None:
            return  # 已清仓
        plan = holding.get("plan") or {}
        new_stop = add_result.get("new_stop_loss")
        if new_stop and new_stop > float(holding.get("stop_loss", 0) or 0):
            # 移动止盈: 上移止损位 + 规划历史追加
            plan.setdefault("history", []).append({
                "time": datetime.now(timezone.utc).isoformat(),
                "action": reduce_result.get("action", "hold"),
                "executed": executed,
                "stop_loss_update": float(new_stop),
            })
            db_ops.update_holding_plan(symbol, plan)
            db_ops.upsert_holding({
                "symbol": symbol, "stop_loss": float(new_stop),
            })
            self.log(
                "plan_updated", symbol=symbol,
                reason=f"止损位上移至 {new_stop} (收盘确认)",
            )
