"""买入 Agent 集群 (设计文档 §7.2, 队列: buy).

流程 (Orchestrator FSM 驱动, 每只自选股一个 flow):
    buy_scan(入口) → 逻辑崩塌检测 → 技术面信号 → 催化事件
    → 二次验证 → 建仓执行

原则:
- 逻辑崩塌检测优先执行 (§7.2.0): 崩塌 → 移出自选池 + 终止流程
- 技术信号全部多 K 线确认, 过滤盘中毛刺 (§7.2.1)
- 弱催化不能单独触发买入, 必须叠加技术信号 (§7.2.2)
- 二次验证未通过 / LLM 不可用 → 不建仓 (宁缺毋滥)
- 建仓金额 = 可用资金 × 20%, 强制 pre_trade_check (§7.2.4)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel, Field

from .. import db_ops
from ..broker import (
    OrderRequest,
    OrderSide,
    OrderType,
    calc_fee,
    get_broker,
    round_lot,
)
from ..config import BUY_POSITION_RATIO, MAX_HOLDING_COUNT
from ..llm_helper import structured_invoke
from ..news_store import get_news_store
from ..orchestrator import get_orchestrator
from ..risk_control import account_snapshot, pre_trade_check
from .base import BaseAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM 输出 schema
# ---------------------------------------------------------------------------

class LogicCollapseCheck(BaseModel):
    """逻辑崩塌检测结论 (§7.2.0)."""

    collapsed: bool = False
    reason: str = ""
    evidence: str = ""


class CatalystEventItem(BaseModel):
    event_type: str = ""      # 政策类 / 业绩类 / 产品类 / 其他
    description: str = ""
    strength: float = 0.0     # 0-10


class CatalystReport(BaseModel):
    events: list[CatalystEventItem] = Field(default_factory=list)
    bear_cleared: bool = False    # 压制利空是否消失
    bear_cleared_desc: str = ""
    overall_strength: float = 0.0  # 综合催化强度 0-10


class VerificationReport(BaseModel):
    """二次验证 + 情景模拟 + 操作规划 (§7.2.3)."""

    passed: bool = False
    reason: str = ""
    major_bear_resolved: bool = False   # 重大利空是否已消除
    bull_scenario: str = ""
    range_scenario: str = ""
    bear_scenario: str = ""
    risk_reward_ratio: str = ""
    stop_loss_pct: float = 5.0          # 止损百分比 (默认 5% 硬性)
    take_profit_pct: float = 15.0
    add_position_conditions: list[str] = Field(default_factory=list)
    predicted_path: str = ""            # 后期走势预测


# ---------------------------------------------------------------------------
# 买入扫描入口 (调度 → buy_scan → 每股一个 buy flow)
# ---------------------------------------------------------------------------

def handle_buy_scan(task: dict) -> dict:
    """buy 队列 buy_scan handler: 遍历自选池, 逐股启动买入流程 (30min 幂等)."""
    payload = task.get("payload", {})
    window = payload.get("window") or datetime.now().strftime("%Y%m%d%H%M")
    orchestrator = get_orchestrator()

    holdings = {h["symbol"] for h in db_ops.get_holdings()}
    pool = db_ops.get_optional_pool("active")
    started: list[str] = []
    for stock in pool:
        symbol = stock.get("symbol", "")
        if not symbol or symbol in holdings:
            continue  # 已持仓的走持仓维护流程, 不重复建仓
        if len(holdings) + len(started) >= MAX_HOLDING_COUNT:
            break  # 硬性规定: 持仓不能超过 3 支
        flow_id = f"buy_{symbol}_{window}"
        orchestrator.start_flow(
            "buy",
            flow_id=flow_id,
            data={
                "symbol": symbol,
                "name": stock.get("name", ""),
                "industry": stock.get("industry", ""),
                "pool": {  # 入选时的分析结论 (崩塌检测基准)
                    "reason": stock.get("reason", ""),
                    "bull_factors": stock.get("bull_factors", []),
                    "bear_factors": stock.get("bear_factors", []),
                    "stage_judgement": stock.get("stage_judgement", ""),
                    "rise_trigger": stock.get("rise_trigger", ""),
                },
            },
        )
        started.append(symbol)
    return {"started_flows": started, "pool_size": len(pool)}


# ---------------------------------------------------------------------------
# 7.2.0 逻辑崩塌检测 Agent (每 30 分钟扫描时优先执行)
# ---------------------------------------------------------------------------

class LogicCollapseAgent(BaseAgent):
    """检查自选股上涨逻辑是否依然成立; 崩塌 → status='removed' + 终止流程."""

    agent_name = "logic_guard"

    # 技术面长期破位硬规则: 收盘价跌破年线 5% 以上 (数据驱动, 不依赖 LLM)
    YEAR_LINE_BREAK_RATIO = 0.95

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")
        symbol = context.get("symbol", "")
        pool = context.get("pool") or {}
        if not symbol:
            return {"decision": "ignored", "reason": "no symbol"}

        # 1) 硬规则: 技术面长期破位 (跌破年线且无反弹迹象)
        indicators = self.data.get_stock_indicators(symbol)
        if indicators:
            close = indicators.get("close_series") or []
            year_line = indicators.get("close_200_sma")
            if close and year_line and close[-1] < year_line * self.YEAR_LINE_BREAK_RATIO:
                return self._collapse(
                    symbol, task_id, flow_id, flow_type, step,
                    f"技术面长期破位: 收盘 {close[-1]} 跌破年线 {round(year_line, 2)} 超过 5%",
                )

        # 2) LLM 判断: 利多消失 / 新重大利空 / 行业景气度逆转 / 政策转向
        news = self.data.get_stock_news(symbol, hours=72)
        stored = get_news_store().search(
            f"{symbol} 利空 风险 业绩 处罚", days=7, n_results=5, symbol=symbol,
        )
        fundamentals = self.data.get_fundamentals_text(symbol)
        news_digest = "\n".join(
            f"- [{n.get('time', n.get('pub_time', ''))}] {n.get('title', '')}"
            for n in (news + stored)[:12]
        )
        prompt = (
            f"自选股 {symbol} 入选时的投资逻辑如下:\n"
            f"入选理由: {pool.get('reason', '')}\n"
            f"主要利多: {pool.get('bull_factors', [])}\n"
            f"主要利空: {pool.get('bear_factors', [])}\n"
            f"阶段判断: {pool.get('stage_judgement', '')}\n"
            f"上涨触发: {pool.get('rise_trigger', '')}\n\n"
            f"最新新闻:\n{news_digest or '(无)'}\n\n"
            f"最新基本面:\n{(fundamentals or '(无)')[:1200]}\n\n"
            "请判断该股的上涨逻辑是否已经崩塌。崩塌标准 (任一满足):\n"
            "1. 核心利多因素消失或减弱 (产品涨价逻辑被证伪 / 政策支持取消)\n"
            "2. 出现新的重大利空 (业绩暴雷 / 监管处罚 / 核心客户流失)\n"
            "3. 行业景气度由上升转为下降\n"
            "4. 政策转向不利于该行业\n"
            "注意: 正常波动和一般性旧闻不算崩塌。输出 collapsed/reason/evidence。"
        )
        try:
            check = structured_invoke(
                self.llm.pick(), LogicCollapseCheck, prompt,
                default=LogicCollapseCheck(),
            )
        except Exception as exc:
            # LLM 不可用: 硬规则已通过, 放行但记录降级 (不因 LLM 故障误删自选股)
            logger.warning("Logic collapse LLM check failed for %s: %s", symbol, exc)
            result = {
                "decision": "active", "llm_check": "unavailable",
                "hard_rule_passed": True,
            }
            self.report(flow_type, flow_id, step, result)
            return result

        if check.collapsed:
            return self._collapse(
                symbol, task_id, flow_id, flow_type, step,
                f"{check.reason} (证据: {check.evidence[:200]})",
            )

        result = {
            "decision": "active",
            "llm_check": "passed",
            "note": check.reason[:200],
        }
        self.report(flow_type, flow_id, step, result)
        return result

    def _collapse(
        self, symbol: str, task_id: str, flow_id: str,
        flow_type: str, step: str, reason: str,
    ) -> dict:
        db_ops.update_optional_status(symbol, "removed", remove_reason=reason)
        self.log(
            "remove", symbol=symbol, task_id=task_id, flow_id=flow_id,
            reason=f"上涨逻辑崩塌, 移出自选池: {reason}",
        )
        result = {
            "decision": "collapse_removed",
            "symbol": symbol,
            "remove_reason": reason,
            "abort": True,
            "abort_reason": f"逻辑崩塌移除: {reason}",
        }
        self.report(flow_type, flow_id, step, result)
        return result


# ---------------------------------------------------------------------------
# 7.2.1 技术面信号 Agent (多 K 线确认, 过滤盘中毛刺)
# ---------------------------------------------------------------------------

class TechSignalAgent(BaseAgent):
    """技术买入信号检测: BOLL 中轨缩量止跌 / MACD 底背离 / 回踩缺口缩量."""

    agent_name = "tech_signal"

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")
        symbol = context.get("symbol", "")

        indicators = self.data.get_stock_indicators(symbol)
        if not indicators:
            # 指标不可用不视为失败 — 本周期无信号
            result = {
                "decision": "no_signal", "passed": False,
                "reason": "技术指标数据不可用", "abort": True,
                "abort_reason": "技术指标数据不可用, 本周期跳过",
            }
            self.report(flow_type, flow_id, step, result)
            return result

        signals = {
            "boll_mid_shrink_stop": self._boll_mid_shrink_stop(indicators),
            "macd_divergence": self._macd_divergence(indicators),
            "gap_pullback_shrink": self._gap_pullback_shrink(indicators),
        }
        triggered = {k: v for k, v in signals.items() if v.get("triggered")}
        passed = bool(triggered)

        result = {
            "decision": "signal" if passed else "no_signal",
            "passed": passed,
            "signals": signals,
            "close": (indicators.get("close_series") or [None])[-1],
        }
        self.log(
            "tech_signal" if passed else "tech_no_signal",
            symbol=symbol, task_id=task_id, flow_id=flow_id,
            reason=(
                "触发: " + "; ".join(f"{k} ({v['detail']})" for k, v in triggered.items())
                if passed else "三种技术形态均未触发"
            ),
            detail=signals,
        )
        # 无论是否触发都正常上报 — 催化事件步骤做最终触发综合判断
        self.report(flow_type, flow_id, step, result)
        return result

    # -- 信号 1: BOLL 中轨缩量止跌 --------------------------------------------

    @staticmethod
    def _boll_mid_shrink_stop(ind: dict) -> dict:
        """连续 2 根 K 线缩量 + 价格未创新低 + 回踩中轨附近."""
        vol = ind.get("volume_series") or []
        close = ind.get("close_series") or []
        low = ind.get("low_series") or []
        boll = ind.get("boll")
        if len(vol) < 3 or len(close) < 3 or len(low) < 4 or not boll:
            return {"triggered": False, "detail": "数据不足"}
        shrinking = vol[-1] < vol[-2] < vol[-3]           # 连续两根缩量
        not_new_low = low[-1] >= min(low[-5:-1]) if len(low) >= 5 else low[-1] >= min(low[:-1])
        touch_mid = min(low[-3:]) <= boll * 1.02 and close[-1] >= boll * 0.97
        triggered = shrinking and not_new_low and touch_mid
        return {
            "triggered": triggered,
            "detail": (
                f"缩量={shrinking}, 未创新低={not_new_low}, 触中轨={touch_mid} "
                f"(close={close[-1]}, boll_mid={round(boll, 2)})"
            ),
        }

    # -- 信号 2: MACD 底背离 (需 RSI 超卖或 KDJ 低位金叉二次确认) ----------------

    @staticmethod
    def _macd_divergence(ind: dict) -> dict:
        close = ind.get("close_series") or []
        macd = ind.get("macd_series") or []
        rsi = ind.get("rsi")
        kdjk = ind.get("kdjk_series") or []
        kdjd = ind.get("kdjd_series") or []
        if len(close) < 3 or len(macd) < 3:
            return {"triggered": False, "detail": "数据不足"}
        price_new_low = close[-1] <= min(close[:-1])
        macd_higher = macd[-1] > min(macd[:-1])
        divergence = price_new_low and macd_higher
        rsi_oversold = rsi is not None and rsi < 35
        kdj_cross = (
            len(kdjk) >= 2 and len(kdjd) >= 2
            and kdjk[-1] > kdjd[-1] and kdjk[-2] <= kdjd[-2]   # 低位金叉
            and kdjk[-1] < 30
        )
        confirmed = rsi_oversold or kdj_cross
        triggered = divergence and confirmed
        return {
            "triggered": triggered,
            "detail": (
                f"背离={divergence}, RSI超卖={rsi_oversold}, KDJ金叉={kdj_cross} "
                f"(rsi={rsi}, macd={macd[-1] if macd else None})"
            ),
        }

    # -- 信号 3: 回踩前期上涨缺口缩量止跌 ---------------------------------------

    @staticmethod
    def _gap_pullback_shrink(ind: dict) -> dict:
        high = ind.get("high_series") or []
        low = ind.get("low_series") or []
        close = ind.get("close_series") or []
        vol = ind.get("volume_series") or []
        if len(high) < 6 or len(low) < 6 or not close:
            return {"triggered": False, "detail": "数据不足"}
        # 找最近向上缺口: low[i] > high[i-1]
        gap = None
        for i in range(len(high) - 2, 0, -1):
            if low[i] > high[i - 1] * 1.005:  # 至少 0.5% 缺口
                gap = {"index": i, "upper": low[i], "lower": high[i - 1]}
                break
        if gap is None:
            return {"triggered": False, "detail": "近 10 日无向上缺口"}
        # 缺口未被实体封闭: 之后最低价未跌破缺口下沿
        not_filled = min(low[gap["index"]:]) > gap["lower"]
        # 当前回踩缺口上沿附近 (距离 < 3%)
        pullback = abs(close[-1] - gap["upper"]) / close[-1] < 0.03 if close[-1] else False
        shrinking = len(vol) >= 2 and vol[-1] < vol[-2]
        triggered = not_filled and pullback and shrinking
        return {
            "triggered": triggered,
            "detail": (
                f"缺口[{gap['lower']}, {gap['upper']}], 未封闭={not_filled}, "
                f"回踩={pullback}, 缩量={shrinking}"
            ),
        }


# ---------------------------------------------------------------------------
# 7.2.2 催化事件 Agent
# ---------------------------------------------------------------------------

class CatalystAgent(BaseAgent):
    """催化事件检测 + 利空解除检查; 与技术信号综合判定买入触发."""

    agent_name = "catalyst_event"
    STRONG_CATALYST_THRESHOLD = 6.0   # 强催化阈值 (弱催化不能单独触发买入)

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")
        symbol = context.get("symbol", "")
        tech = context.get("tech_signal") or {}
        tech_passed = bool(tech.get("passed"))

        news = self.data.get_stock_news(symbol, hours=72)
        stored = get_news_store().search(
            f"{symbol} 催化 利好 政策 业绩 订单", days=7, n_results=5, symbol=symbol,
        )
        lockup = self.data.get_lockup_expiry_text(symbol)
        insider = self.data.get_insider_text(symbol)

        catalyst = self._llm_catalyst(symbol, news + stored, lockup, insider)

        # 综合触发判定 (§2.2 + §7.2.2):
        # - 技术信号通过 → 触发成立 (催化作为附加确认)
        # - 技术信号未通过 → 需强催化 + 利空压制消失
        triggered = tech_passed or (
            catalyst is not None
            and catalyst.overall_strength >= self.STRONG_CATALYST_THRESHOLD
            and catalyst.bear_cleared
        )

        result = {
            "decision": "triggered" if triggered else "no_trigger",
            "triggered": triggered,
            "tech_passed": tech_passed,
            "catalyst": (
                catalyst.model_dump() if catalyst is not None
                else {"available": False}
            ),
        }
        self.log(
            "catalyst_triggered" if triggered else "catalyst_no_trigger",
            symbol=symbol, task_id=task_id, flow_id=flow_id,
            reason=(
                f"技术信号={'通过' if tech_passed else '未通过'}, "
                f"催化强度={catalyst.overall_strength if catalyst else 'N/A'}, "
                f"利空消失={catalyst.bear_cleared if catalyst else 'N/A'}"
            ),
            detail=result,
        )
        if not triggered:
            result["abort"] = True
            result["abort_reason"] = "无买入触发条件 (技术信号未通过且无强催化+利空消失)"
        self.report(flow_type, flow_id, step, result)
        return result

    def _llm_catalyst(
        self, symbol: str, news: list[dict], lockup: str, insider: str,
    ) -> Optional[CatalystReport]:
        if not self.llm.available:
            return None
        news_digest = "\n".join(
            f"- [{n.get('time', n.get('pub_time', ''))}] {n.get('title', '')}: "
            f"{(n.get('content', '') or '')[:100]}"
            for n in news[:12]
        )
        prompt = (
            f"分析 A 股 {symbol} 近期是否存在正向催化事件或利空解除。\n\n"
            f"## 新闻\n{news_digest or '(无)'}\n\n"
            f"## 解禁日程\n{(lockup or '(无)')[:300]}\n\n"
            f"## 高管增减持\n{(insider or '(无)')[:300]}\n\n"
            "输出 JSON: events(催化事件列表, 每项含 event_type[政策类/业绩类/产品类/其他], "
            "description, strength[0-10 结合历史胜率从严评分]), bear_cleared(此前压制股价的"
            "利空是否已消失, 如解禁结束/诉讼和解/政策落地), bear_cleared_desc, "
            "overall_strength(综合催化强度 0-10, 无催化为 0)."
        )
        try:
            return structured_invoke(
                self.llm.pick(), CatalystReport, prompt,
                default=CatalystReport(),
            )
        except Exception as exc:
            logger.warning("Catalyst LLM analysis failed for %s: %s", symbol, exc)
            return None


# ---------------------------------------------------------------------------
# 7.2.3 二次验证 Agent
# ---------------------------------------------------------------------------

class SecondVerificationAgent(BaseAgent):
    """重跑深度分析逻辑: 重大利空检查 + 三情景推演 + 操作规划."""

    agent_name = "second_verification"

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")
        symbol = context.get("symbol", "")
        pool = context.get("pool") or {}
        tech = context.get("tech_signal") or {}
        catalyst = (context.get("catalyst_event") or {}).get("catalyst") or {}

        # 辅助数据获取容错: 新闻/基本面/行情任一数据源故障不阻断验证步骤,
        # 降级为空数据继续 (避免任务重试进死信后 flow 挂起; LLM 关卡仍必经)
        try:
            news = self.data.get_stock_news(symbol, hours=72)
            fundamentals = self.data.get_fundamentals_text(symbol)
            quote = self.data.get_realtime_quote(symbol)
        except Exception as exc:
            logger.warning("Second verification data fetch degraded for %s: %s", symbol, exc)
            news, fundamentals, quote = [], "", None
        news_digest = "\n".join(
            f"- [{n.get('time', '')}] {n.get('title', '')}" for n in news[:10]
        )

        prompt = (
            f"对 A 股 {symbol} ({(quote or {}).get('name', '')}, 现价 "
            f"{(quote or {}).get('price', '?')}) 进行买入前二次验证。\n\n"
            f"## 入选时的分析\n入选理由: {pool.get('reason', '')}\n"
            f"利空清单: {pool.get('bear_factors', [])}\n"
            f"阶段判断: {pool.get('stage_judgement', '')}\n\n"
            f"## 触发情况\n技术信号: {tech.get('signals', {})}\n"
            f"催化事件: {catalyst}\n\n"
            f"## 最新新闻\n{news_digest or '(无)'}\n\n"
            f"## 基本面\n{(fundamentals or '(无)')[:1200]}\n\n"
            "要求:\n"
            "1. 检查入选时利空清单中的重大利空是否已消除, 未消除直接拒绝\n"
            "2. 三种市场情景 (上涨/震荡/下跌) 推演买入后的走势\n"
            "3. 评估风险收益比, 不达标拒绝\n"
            "4. 生成操作规划: 止损位百分比(默认5%, A股硬性止损)、止盈百分比、加仓条件列表\n"
            "输出 JSON: passed, reason, major_bear_resolved, bull_scenario, "
            "range_scenario, bear_scenario, risk_reward_ratio, stop_loss_pct, "
            "take_profit_pct, add_position_conditions, predicted_path(后期走势预测)."
        )
        try:
            report = structured_invoke(
                self.llm.pick(deep=True), VerificationReport, prompt, deep=True,
            )
        except Exception as exc:
            # LLM 不可用 → 拒绝建仓 (二次验证是必经关卡, 不可降级跳过)
            logger.warning("Second verification LLM failed for %s: %s", symbol, exc)
            result = {
                "decision": "rejected", "passed": False,
                "reason": "二次验证 LLM 不可用, 宁缺毋滥拒绝建仓",
                "abort": True, "abort_reason": "二次验证不可用",
            }
            self.log(
                "verification_unavailable", symbol=symbol,
                task_id=task_id, flow_id=flow_id,
                reason=result["reason"],
            )
            self.report(flow_type, flow_id, step, result)
            return result

        passed = bool(report.passed and report.major_bear_resolved)
        result = {
            "decision": "passed" if passed else "rejected",
            "passed": passed,
            "reason": report.reason,
            "major_bear_resolved": report.major_bear_resolved,
            "scenarios": {
                "bull": report.bull_scenario,
                "range": report.range_scenario,
                "bear": report.bear_scenario,
            },
            "risk_reward_ratio": report.risk_reward_ratio,
            "plan": {
                "stop_loss_pct": max(float(report.stop_loss_pct or 5.0), 1.0),
                "take_profit_pct": max(float(report.take_profit_pct or 15.0), 5.0),
                "add_position_conditions": report.add_position_conditions,
            },
            "predicted_path": report.predicted_path,
        }
        self.log(
            "verification_passed" if passed else "verification_rejected",
            symbol=symbol, task_id=task_id, flow_id=flow_id,
            reason=report.reason[:300], detail=result,
        )
        if not passed:
            result["abort"] = True
            result["abort_reason"] = f"二次验证未通过: {report.reason[:200]}"
        self.report(flow_type, flow_id, step, result)
        return result


# ---------------------------------------------------------------------------
# 7.2.4 建仓执行 Agent
# ---------------------------------------------------------------------------

class PositionOpenAgent(BaseAgent):
    """执行建仓: 20% 资金 → pre_trade_check 强制校验 → Broker 下单 → 持仓池."""

    agent_name = "position_open"

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")
        symbol = context.get("symbol", "")
        name = context.get("name", "")
        verification = context.get("second_verification") or {}
        plan = verification.get("plan") or {}

        if not verification.get("passed"):
            result = {
                "decision": "rejected", "abort": True,
                "abort_reason": "二次验证未通过, 不建仓",
            }
            self.report(flow_type, flow_id, step, result)
            return result

        # 1) 计算买入金额: 可用资金 × 20% (§2.2)
        snapshot = account_snapshot()
        budget = snapshot.get("available_cash", 0.0) * BUY_POSITION_RATIO
        try:
            quote = self.data.get_realtime_quote(symbol)
        except Exception as exc:
            logger.warning("Position open quote fetch failed for %s: %s", symbol, exc)
            quote = None
        if not quote or budget <= 0:
            result = {
                "decision": "rejected", "abort": True,
                "abort_reason": "行情不可用或可用资金不足",
            }
            self.log("open_rejected", symbol=symbol, task_id=task_id,
                     flow_id=flow_id, reason=result["abort_reason"])
            self.report(flow_type, flow_id, step, result)
            return result

        # 2) 数量计算: 预扣滑点与手续费的缓冲
        est_price = quote["price"] * 1.002  # 滑点 + 缓冲
        qty = round_lot(int(budget / est_price / 100) * 100)
        if qty <= 0:
            result = {
                "decision": "rejected", "abort": True,
                "abort_reason": f"预算 {budget:.0f} 元不足一手",
            }
            self.report(flow_type, flow_id, step, result)
            return result
        est_amount = est_price * qty
        est_fee = calc_fee(est_amount, OrderSide.BUY)

        # 3) 风控同步前置校验 (第一道防线, 强制; §7.2.4)
        order = OrderRequest(
            symbol=symbol, side=OrderSide.BUY, quantity=qty,
            order_type=OrderType.MARKET,
            reason=f"建仓: {verification.get('reason', '')[:200]}",
            flow_id=flow_id,
        )
        check = pre_trade_check(order, snapshot)
        if not check.ok:
            result = {
                "decision": "risk_rejected", "abort": True,
                "abort_reason": f"pre_trade_check 拒绝: {check.reason}",
            }
            self.log(
                "open_risk_rejected", symbol=symbol, task_id=task_id,
                flow_id=flow_id, reason=check.reason, detail=check.to_dict(),
            )
            self.report(flow_type, flow_id, step, result)
            return result

        # 4) Broker 下单 (内部写 trade_log)
        result_order = get_broker().place_order(order)
        if result_order.status.value != "filled":
            result = {
                "decision": "order_failed", "abort": True,
                "abort_reason": f"下单未成交: {result_order.message}",
                "order_status": result_order.status.value,
            }
            self.log(
                "open_order_failed", symbol=symbol, task_id=task_id,
                flow_id=flow_id, reason=result_order.message,
            )
            self.report(flow_type, flow_id, step, result)
            return result

        fill_price = result_order.avg_fill_price
        # 含费成本: 与 broker 内存态/用户账户口径统一 (避免盈亏系统性高估)
        cost_price = round(
            (result_order.amount + result_order.fee) / result_order.filled_quantity, 4,
        )
        stop_loss_pct = float(plan.get("stop_loss_pct", 5.0))
        take_profit_pct = float(plan.get("take_profit_pct", 15.0))

        # 5) 写持仓池: T+1 禁卖标记 + 操作规划 + 走势预测 (§7.2.4)
        # 复用 user_accounts._t1_lock_until: 市场时区下一交易日开盘, 全链路口径一致
        from ..user_accounts import _t1_lock_until
        cannot_sell_until = _t1_lock_until()
        db_ops.upsert_holding({
            "symbol": symbol,
            "name": name or quote.get("name", ""),
            "quantity": result_order.filled_quantity,
            "available_quantity": 0,  # T+1: 当日买入冻结
            "cost_price": cost_price,
            "entry_reason": verification.get("reason", ""),
            "plan": plan,
            "predicted_path": {"text": verification.get("predicted_path", "")},
            "stop_loss": round(fill_price * (1 - stop_loss_pct / 100), 3),
            "take_profit": round(fill_price * (1 + take_profit_pct / 100), 3),
            "cannot_sell_until": cannot_sell_until,
        })
        # 自选池标记 bought
        db_ops.update_optional_status(symbol, "bought")

        stop_loss_price = round(fill_price * (1 - stop_loss_pct / 100), 3)
        take_profit_price = round(fill_price * (1 + take_profit_pct / 100), 3)

        # 6) 多用户扇出: 同一建仓决策在每个用户模拟账户独立执行 (§多用户持仓池)
        from ..user_accounts import fanout_buy
        fanout_summary = fanout_buy(
            symbol=symbol,
            price=fill_price,
            name=name or quote.get("name", ""),
            reason=verification.get("reason", "")[:200],
            plan=plan,
            stop_loss=stop_loss_price,
            take_profit=take_profit_price,
            flow_id=flow_id,
        )

        result = {
            "decision": "filled",
            "symbol": symbol,
            "order_id": result_order.order_id,
            "price": fill_price,
            "quantity": result_order.filled_quantity,
            "amount": result_order.amount,
            "fee": result_order.fee,
            "stop_loss": stop_loss_price,
            "take_profit": take_profit_price,
            "cannot_sell_until": cannot_sell_until.isoformat(),
            "user_fanout": fanout_summary,
        }
        self.log(
            "position_opened", symbol=symbol, task_id=task_id, flow_id=flow_id,
            reason=f"建仓 ×{result_order.filled_quantity} @ {fill_price}",
            detail=result,
        )
        self.report(flow_type, flow_id, step, result)
        return result
