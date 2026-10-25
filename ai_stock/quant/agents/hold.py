"""持仓维护 Agent 集群 (设计文档 §7.3, 队列: hold).

流程 (Orchestrator FSM 驱动, 每只持仓一个 flow):
    hold_scan(入口) → 走势跟踪 → 加仓决策 → 减仓/清仓决策 → 执行与规划更新

原则:
- 止损按实时价判定, 破位即卖 (§7.3.1) —— 等收盘确认等于只能次日开盘卖, 跳空风险更大
- 移动止盈的上移需收盘确认 (§7.3.2)
- 时间止损严格按 A 股交易日计数 (calendar_utils)
- 硬性规则: 亏损超 5% 清仓; 5 个交易日无大行情清仓 (规则引擎硬规则)
- 新增重大利空可跳过技术指标直接清仓建议 (§7.3.3, news_store 检索)
- 全部调仓指令强制 pre_trade_check (§7.3.4); 被拒/未成交写退出意图, 按冷却重试
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel

from .. import db_ops, exit_state
from ..broker import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    get_broker,
    round_lot,
)
from ..calendar_utils import count_trading_days
from ..config import (
    BUY_POSITION_RATIO,
    MAJOR_BEAR_BYPASS_DROP,
    SINGLE_POSITION_MAX_PCT,
    STOP_LOSS_ATR_MULT,
    STOP_LOSS_CONFIRM_BARS,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PARTIAL_PORTION,
    TIME_STOP_DAYS_BY_STAGE,
    TIME_STOP_MIN_GAIN,
    TIME_STOP_TRADING_DAYS,
    TRAILING_STOP_FLOOR,
    TRAILING_STOP_TIERS,
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


def _daily_change_ratio(quote: dict) -> Optional[float]:
    """当日涨跌幅 (小数). 行情缺 ``change_pct`` 时返回 None, 由调用方决定降级行为."""
    change_pct = quote.get("change_pct")
    if change_pct is None:
        return None
    try:
        return round(float(change_pct) / 100.0, 4)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 持仓扫描入口
# ---------------------------------------------------------------------------

def handle_hold_scan(task: dict) -> dict:
    """hold 队列 hold_scan handler: 遍历持仓, 逐只启动维护流程 (10min 幂等).

    退出意图 (``pending_exit``) 优先: 已有「决定卖但没卖掉」的持仓不再等 10 分钟
    重新决策, 而是按冷却 (30 分钟) 直接重试, 直到成交或当日次数用尽。
    """
    payload = task.get("payload", {})
    window = payload.get("window") or datetime.now().strftime("%Y%m%d%H%M")
    orchestrator = get_orchestrator()

    started: list[str] = []
    cooling: list[str] = []
    for holding in db_ops.get_holdings():
        symbol = holding.get("symbol", "")
        if not symbol:
            continue
        pending = exit_state.get_pending_exit(holding) if exit_state.enabled() else None
        force_exit = pending if exit_state.is_actionable(pending) else None
        if force_exit and not exit_state.should_retry(force_exit):
            # 冷却中/当日次数用尽: 不起 flow —— 否则每隔 10 分钟就重复打一次
            # pre_trade_check 与告警, 日志噪音会把真正的问题埋掉。
            cooling.append(symbol)
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
                "force_exit": force_exit,
            },
        )
        started.append(symbol)
    return {"started_flows": started, "retry_cooling": cooling}


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
        low_series = indicators.get("low_series") or []
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
        # 建仓阶段 (HEAD/BODY) 决定时间止损窗口 (T5): 主线鱼头横盘蓄势期更长,
        # 固定 5 日会把主升浪前的洗盘当成"无行情"清掉。
        stage = str((context.get("plan") or {}).get("stage") or "BODY").upper()
        time_stop_days = int(
            TIME_STOP_DAYS_BY_STAGE.get(stage, TIME_STOP_TRADING_DAYS)
        ) or TIME_STOP_TRADING_DAYS

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

        # 止损信号 (T4): 止损位 = min(成本 × (1-5%), 成本 - 2×ATR)。
        # **按实时价判定, 破位即卖** —— 不是等收盘确认: 非交易时段一律拒单,
        # "收盘确认"实际意味着只能次日开盘卖, 隔夜跳空的风险大于盘中假破位
        # (原实现要求 2 根确认, 急跌中实际亏损达 8~12%)。
        if atr_ratio > 0 and atr > 0:
            dynamic_stop = min(
                cost * (1 - STOP_LOSS_PCT), cost - STOP_LOSS_ATR_MULT * atr,
            )
        else:
            dynamic_stop = cost * (1 - STOP_LOSS_PCT)
        confirm_bars = max(1, STOP_LOSS_CONFIRM_BARS)
        recent_close = close_series[-confirm_bars:]
        stop_confirmed = bool(
            recent_close
            and len(close_series) >= confirm_bars
            and all(c <= dynamic_stop for c in recent_close)
        )
        # 实时价破位: 不能只依赖 close 序列。日线与实时两源不同步时 provisional
        # bar 拼不上, close_series[-1] 会是昨天的收盘 → 止损整整滞后一天。
        live_price = float(quote.get("price") or 0.0)
        intraday_stop = bool(live_price > 0 and live_price <= dynamic_stop)
        stop_loss_signal = "sell_all" in rule_actions or stop_confirmed or intraday_stop
        # 观测字段 (不是卖出判据): 今日最低已破位、现价又收回止损位之上 ——
        # 用来复盘「假摔」比例, 判断止损位是否过紧。
        intraday_breach = bool(
            not stop_loss_signal
            and low_series
            and len(low_series) == len(close_series)
            and low_series[-1] <= dynamic_stop < current_close
        )
        # 时间止损信号 (分层窗口)
        time_stop_signal = "sell_all" in rule_actions and holding_trading_days >= time_stop_days
        if not time_stop_signal:
            time_stop_signal = (
                holding_trading_days >= time_stop_days
                and max_gain_ratio < TIME_STOP_MIN_GAIN
                and atr_ratio < 0.04
            )

        # 走势对比: 与买入时预测路径对比 (LLM 预测文本 + 简单数据对比)
        predicted = (context.get("predicted_path") or {}).get("text", "")
        trend = "as_expected"
        if gain_ratio >= 0.08 or (
            "sell_all" not in rule_actions and max_gain_ratio >= 0.10
        ):
            trend = "above"       # 超预期
        elif loss_ratio >= STOP_LOSS_PCT * 0.6:
            trend = "below"       # 不及预期

        result = {
            "decision": "tracked",
            "symbol": symbol,
            "current_price": quote.get("price"),
            "current_close": current_close,
            # 当日涨跌幅 (相对昨收): 利空旁路判据用, 不能用 loss_ratio 代替 ——
            # loss_ratio 是相对成本, 与「今天跌了多少」是两件事
            "daily_change_ratio": _daily_change_ratio(quote),
            "loss_ratio": round(loss_ratio, 4),
            "gain_ratio": round(gain_ratio, 4),
            "max_gain_ratio": round(max_gain_ratio, 4),
            "atr_ratio": round(atr_ratio, 4),
            "holding_trading_days": holding_trading_days,
            "time_stop_days": time_stop_days,
            "stage_batch": stage,
            "trend": trend,
            "dynamic_stop_price": round(dynamic_stop, 3),
            "stop_loss_signal": stop_loss_signal,
            "intraday_breach": intraday_breach,
            "time_stop_signal": time_stop_signal,
            "triggered_rules": triggered_rules,
            "predicted_path": predicted[:300],
        }
        self.log(
            "trend_tracked", symbol=symbol, task_id=task_id, flow_id=flow_id,
            reason=(
                f"trend={trend}, 浮亏={loss_ratio:.1%}, 持有{holding_trading_days}交易日, "
                f"动态止损位={dynamic_stop:.2f}, 止损信号={stop_loss_signal}, "
                f"盘中破位={intraday_breach}, 时间止损={time_stop_signal}"
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

        # 突破关键位置: 收盘突破近 10 个交易日高点 (前高, 不含当日)
        breakout = bool(
            len(high_series) >= 11
            and current_close > max(high_series[-11:-1])
        )
        above_expectation = trend_result.get("trend") == "above"
        new_catalyst = self._check_new_catalyst(symbol, context)

        should_add = bool(breakout or above_expectation or new_catalyst) and \
            not trend_result.get("stop_loss_signal") and \
            not trend_result.get("time_stop_signal")

        # 移动止盈 (§7.3.2): 收盘确认的浮盈达到档位 → 止损位阶梯上移。
        # 只上移不下移: 现价回落后档位不再满足, new_stop_loss 保持 None,
        # 已有止损位由 _update_plan 的 "new_stop > 当前值" 判断兜底。
        new_stop_loss = None
        current_stop = float(context.get("stop_loss") or 0.0)
        tiers = TRAILING_STOP_TIERS if db_ops.get_flag(
            "trailing_tiers_enabled", True,
        ) else ((TRAILING_STOP_TRIGGER, TRAILING_STOP_FLOOR),)
        if current_close and cost:
            for trigger, floor in tiers:
                if gain_ratio >= trigger:
                    new_stop_loss = round(cost * (1 + floor), 3)
        if new_stop_loss is not None and new_stop_loss <= current_stop:
            new_stop_loss = None
        if new_stop_loss is not None:
            self.log(
                "trailing_stop_raised", symbol=symbol, task_id=task_id,
                flow_id=flow_id,
                reason=f"收盘确认浮盈 {gain_ratio:.1%}, 止损上移至 {new_stop_loss} "
                       f"(原 {current_stop or '未设置'})",
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

        action, portion, reason = "hold", 0.0, ""

        # 0) 退出意图重试: 上一轮已决定卖出但没成交 (风控拒单 / 跌停挂单)。
        #    价格已破位, 不能因为当日反弹就撤销退出, 所以跳过 LLM 与趋势判断。
        force = context.get("force_exit")
        if force:
            portion = float(force.get("portion") or 1.0)
            action = "sell_all" if portion >= 1.0 else "sell_partial"
            reason = (
                f"退出意图重试: {force.get('reason') or '上一轮未成交'}"
                f" (累计失败 {int(force.get('fail_count') or 0)} 次)"
            )
        # 1) 硬性规则 (§2.3): 亏损超 5% / 5 交易日无大行情 → 直接清仓
        elif trend_result.get("stop_loss_signal"):
            action, portion = "sell_all", 1.0
            reason = (
                f"硬性止损: 浮亏 {trend_result.get('loss_ratio', 0):.1%} "
                f"(实时价跌破动态止损位 {trend_result.get('dynamic_stop_price')}, "
                f"ATR 挂钩)"
            )
        elif trend_result.get("time_stop_signal"):
            action, portion = "sell_all", 1.0
            reason = (
                f"时间止损: 持有 {trend_result.get('holding_trading_days', 0)} 个交易日, "
                f"最大涨幅 {trend_result.get('max_gain_ratio', 0):.1%} 无大行情"
            )
        else:
            # 2) 止盈 (在软性条件之前: 止盈不该被利空判断拦下来)
            action, portion, reason = self._take_profit_or_soft_sell(
                symbol, context, trend_result,
            )

        result = {
            "decision": action,
            "action": action,
            "reason": reason,
            "portion": portion,
        }
        self.log(
            f"reduce_{action}", symbol=symbol, task_id=task_id, flow_id=flow_id,
            reason=reason, detail=result,
        )
        self.report(flow_type, flow_id, step, result)
        return result

    def _take_profit_or_soft_sell(
        self, symbol: str, context: dict, trend_result: dict,
    ) -> tuple[str, float, str]:
        """止盈减半 / 新增重大利空 / 走势不及预期 —— 三者按优先级返回决策."""
        take_profit = float(context.get("take_profit") or 0.0)
        price = float(trend_result.get("current_close") or 0.0)
        if (
            db_ops.get_flag("take_profit_partial_enabled", True)
            and take_profit > 0 and price >= take_profit
        ):
            return (
                "sell_partial", TAKE_PROFIT_PARTIAL_PORTION,
                f"触及止盈价 {take_profit} (现价 {price}), "
                f"减仓 {TAKE_PROFIT_PARTIAL_PORTION:.0%} 交由移动止损管理余仓",
            )
        return self._major_bear_decision(symbol, context, trend_result)

    def _major_bear_decision(
        self, symbol: str, context: dict, trend_result: dict,
    ) -> tuple[str, float, str]:
        """重大利空两段式确认 (D5).

        为什么不是「利空就清仓」: LLM 单次判定有采样波动, 一条被标题党放大的
        旧闻就能清掉一个刚建仓的仓位。为什么不是「一律等一轮」: 真利空的第一天
        往往就是大跌, 白亏一天。所以 —— 首次判定只登记待确认, 下一轮再判定为真
        或当日跌幅超过 ``MAJOR_BEAR_BYPASS_DROP`` 才清仓。
        """
        major_bear = self._check_major_bear(symbol, context)
        if major_bear is None or not major_bear.has_major_bear:
            if trend_result.get("trend") == "below":
                return "sell_partial", 0.5, "走势不及预期, 减仓 1/2 降低风险"
            return "hold", 0.0, ""

        headline = f"重大利空: {major_bear.reason}"
        if not db_ops.get_flag("major_bear_confirm_rounds", True):
            # 回滚分支: 单次判定即清仓 (改动前的行为)
            return "sell_all", 1.0, f"{headline}, 跳过技术指标直接清仓"

        pending = exit_state.get_pending_exit(db_ops.get_holding(symbol) or {})
        if pending and pending.get("confirm_needed"):
            return "sell_all", 1.0, f"{headline}, 二次确认成立 → 清仓"

        change = trend_result.get("daily_change_ratio")
        daily_drop = max(0.0, -float(change)) if change is not None else 0.0
        if daily_drop >= MAJOR_BEAR_BYPASS_DROP:
            return (
                "sell_all", 1.0,
                f"{headline}, 当日跌幅 {daily_drop:.1%} ≥ {MAJOR_BEAR_BYPASS_DROP:.0%} "
                f"→ 直接清仓",
            )

        exit_state.mark_pending_exit(
            symbol, reason=f"重大利空待确认: {major_bear.reason}",
            portion=1.0, confirm_needed=1,
        )
        return (
            "hold", 0.0,
            f"{headline}, 首次判定 → 登记待确认, 下一轮再判定为真才清仓",
        )

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
            pending = self._record_blocked_exit(symbol, reduce_result, portion, check.reason)
            self.log(
                "sell_risk_rejected", symbol=symbol, task_id=task_id,
                flow_id=flow_id, reason=f"卖出被风控拒绝: {check.reason}",
                detail={"pending_exit": pending},
            )
            return {
                "op": "sell", "status": "risk_rejected",
                "reason": check.reason, "pending_exit": pending,
            }

        result_order = broker.place_order(order)
        if result_order.status == OrderStatus.PENDING:
            # 跌停挂单排队: 既不是成交也不是终局失败, 记退出意图等下一轮重试
            pending = self._record_blocked_exit(
                symbol, reduce_result, portion, result_order.message, queued=True,
            )
            self.log(
                "sell_pending", symbol=symbol, task_id=task_id, flow_id=flow_id,
                reason=f"卖出未成交: {result_order.message}",
                detail={"pending_exit": pending},
            )
            return {
                "op": "sell", "status": OrderStatus.PENDING.value,
                "reason": result_order.message, "pending_exit": pending,
            }

        outcome = {
            "op": "sell",
            "status": result_order.status.value,
            "quantity": result_order.filled_quantity,
            "price": result_order.avg_fill_price,
            "amount": result_order.amount,
            "fee": result_order.fee,
        }
        if result_order.status.value == "filled":
            # 成交即清除退出意图 (无论之前有没有)
            exit_state.clear_pending_exit(symbol)
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

    def _record_blocked_exit(
        self, symbol: str, reduce_result: dict, portion: float, error: str,
        *, queued: bool = False,
    ) -> Optional[dict]:
        """记录「已决定卖出但没成交」并告警; 开关关闭时返回 None (旧行为)."""
        if not exit_state.enabled():
            return None
        return exit_state.mark_and_notify(
            symbol,
            reason=reduce_result.get("reason", ""),
            portion=portion,
            error=error,
            queued=queued,
        )

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
            # 与建仓同一口径: 实盘 broker 层复核要拿到实际比例, 否则会被 20% 兜底误拒
            extra={"used_ratio": BUY_POSITION_RATIO},
        )
        check = pre_trade_check(order, position_ratio=BUY_POSITION_RATIO)
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
            # 同步持仓池: 数量累加 + 成本含费摊薄; 今日新买份额 T+1 冻结,
            # 可卖数量保持不变 (与 broker 权威数据一致)
            holding = db_ops.get_holding(symbol)
            old_qty = int((holding or {}).get("quantity", 0) or 0)
            old_cost = float((holding or {}).get("cost_price", 0.0) or 0.0)
            new_qty = old_qty + result_order.filled_quantity
            new_cost = (
                (old_cost * old_qty + result_order.amount + result_order.fee) / new_qty
                if new_qty else 0.0
            )
            db_ops.upsert_holding({
                "symbol": symbol,
                "quantity": new_qty,
                "available_quantity": int(
                    (holding or {}).get("available_quantity", 0) or 0
                ),
                "cost_price": round(new_cost, 4),
            })
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
