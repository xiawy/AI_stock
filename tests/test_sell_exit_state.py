"""卖出可靠性回归测试 (FR-02 / FR-13 / FR-06).

修复前的两个失效场景:

- **卖单被拒只记日志**: ``_execute_sell`` 被 ``pre_trade_check`` 拒掉后直接返回,
  下一轮 10 分钟扫描要重新跑一遍完整决策 (含 LLM)。极端行情里连续拒单 = 止损
  完全失效。
- **跌停直接拒卖**: ``risk_control`` 与 ``SimulatedBroker`` 双重拒单, 跌停当天
  根本卖不出去, 也不会留下任何「还没卖掉」的记录。

修复后: 未成交的卖出决定写入持仓 ``plan_json`` 的 ``pending_exit``, 由扫描入口
按 30 分钟冷却重试, 跳过决策直接执行; 跌停改为挂单排队 (PENDING) 且不写
``trade_log`` (否则重试会吃掉 ``max_daily_orders`` 的笔数限额)。
"""

from datetime import datetime, timedelta, timezone

import pytest


TZ8 = timezone(timedelta(hours=8))
SYMBOL = "600519"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def holding(quant_env):
    """建仓记录: 1000 股 @60, 已过 T+1."""
    from ai_stock.quant import db_ops

    db_ops.upsert_holding({
        "symbol": SYMBOL, "name": "贵州茅台", "industry": "白酒",
        "quantity": 1000, "available_quantity": 1000, "cost_price": 60.0,
        "stop_loss": 57.0, "take_profit": 75.0,
    })
    return SYMBOL


@pytest.fixture()
def clock(monkeypatch):
    """可推进的市场时钟 (退出意图冷却/每日次数上限按它计算)."""

    class _Clock:
        def __init__(self):
            self.now = datetime(2026, 10, 20, 10, 30, tzinfo=TZ8)

        def __call__(self):
            return self.now

        def tick(self, **kwargs):
            self.now = self.now + timedelta(**kwargs)
            return self.now

    ticker = _Clock()
    from ai_stock.quant import exit_state

    monkeypatch.setattr(exit_state, "market_now", ticker)
    return ticker


def _agent(market):
    from ai_stock.quant.agents.hold import ExecutionUpdateAgent

    return ExecutionUpdateAgent(data_service=market)


def _sell_context(reason="硬性止损"):
    return {
        "symbol": SYMBOL, "quantity": 1000, "cost_price": 60.0,
        "stop_loss": 57.0, "take_profit": 75.0,
    }


def _reduce(portion=1.0, reason="硬性止损"):
    return {
        "action": "sell_all" if portion >= 1.0 else "sell_partial",
        "portion": portion,
        "reason": reason,
    }


def _quote(price, prev_close, limit_down=None):
    return {
        "price": price, "prev_close": prev_close, "name": "贵州茅台",
        "limit_up": round(prev_close * 1.1, 2),
        "limit_down": limit_down if limit_down is not None else round(prev_close * 0.9, 2),
    }


# ---------------------------------------------------------------------------
# 退出意图存储语义
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPendingExitState:
    def test_mark_accumulates_and_keeps_first_attempt(self, quant_env, holding, clock):
        from ai_stock.quant import exit_state

        first = exit_state.mark_pending_exit(SYMBOL, reason="风控拒单", portion=0.5, error="非交易时段")
        clock.tick(minutes=5)
        second = exit_state.mark_pending_exit(SYMBOL, reason="风控拒单", portion=1.0, error="非交易时段")

        assert first["fail_count"] == 1
        assert second["fail_count"] == 2
        assert second["attempts"] == 2
        assert second["day_attempts"] == 2
        assert second["first_at"] == first["first_at"]
        # 重试必须卖同样的量, 不能因为重复 mark 而漂移
        assert second["portion"] == 0.5
        assert second["last_error"] == "非交易时段"

    def test_confirm_needed_is_not_actionable(self, quant_env, holding, clock):
        from ai_stock.quant import exit_state

        pending = exit_state.mark_pending_exit(
            SYMBOL, reason="重大利空待确认", portion=1.0, confirm_needed=1,
        )
        assert exit_state.is_actionable(pending) is False
        assert exit_state.is_actionable({**pending, "confirm_needed": 0}) is True

    def test_clear_removes_intent(self, quant_env, holding, clock):
        from ai_stock.quant import db_ops, exit_state

        exit_state.mark_pending_exit(SYMBOL, reason="风控拒单", error="x")
        exit_state.clear_pending_exit(SYMBOL)

        assert exit_state.get_pending_exit(db_ops.get_holding(SYMBOL)) is None
        # 其他 plan 字段不能被顺手抹掉
        assert db_ops.get_holding(SYMBOL)["plan"] == {}

    def test_should_retry_respects_cooldown(self, quant_env, holding, clock):
        from ai_stock.quant import exit_state
        from ai_stock.quant.config import SELL_RETRY_COOLDOWN_MINUTES

        pending = exit_state.mark_pending_exit(SYMBOL, reason="风控拒单", error="x")
        assert exit_state.should_retry(pending) is False

        clock.tick(minutes=SELL_RETRY_COOLDOWN_MINUTES - 1)
        assert exit_state.should_retry(pending) is False

        clock.tick(minutes=2)
        assert exit_state.should_retry(pending) is True

    def test_should_retry_stops_at_daily_cap(self, quant_env, holding, clock):
        from ai_stock.quant import exit_state
        from ai_stock.quant.config import (
            SELL_RETRY_COOLDOWN_MINUTES,
            SELL_RETRY_MAX_PER_DAY,
        )

        pending = None
        for _ in range(SELL_RETRY_MAX_PER_DAY):
            pending = exit_state.mark_pending_exit(SYMBOL, reason="风控拒单", error="x")
            clock.tick(minutes=SELL_RETRY_COOLDOWN_MINUTES + 1)

        assert pending["day_attempts"] == SELL_RETRY_MAX_PER_DAY
        assert exit_state.should_retry(pending) is False

        # 跨日重置当日计数
        clock.tick(days=1)
        assert exit_state.should_retry(pending) is True
        refreshed = exit_state.mark_pending_exit(SYMBOL, reason="风控拒单", error="x")
        assert refreshed["day_attempts"] == 1
        assert refreshed["attempts"] == SELL_RETRY_MAX_PER_DAY + 1

    def test_switch_off_means_disabled(self, quant_env, holding, clock):
        from ai_stock.quant import db_ops, exit_state

        assert exit_state.enabled() is True
        db_ops.set_config_value("sell_retry_enabled", "0")
        assert exit_state.enabled() is False


# ---------------------------------------------------------------------------
# 告警: 失败次数分级 + 告警类型按标的隔离
# ---------------------------------------------------------------------------


class _Recorder:
    """替代 AlertManager: 只记录调用, 不做静默窗口去重 (便于断言每次分级)。"""

    def __init__(self):
        self.events: list[tuple[str, str, str]] = []

    def emit(self, level, alert_type, message):
        self.events.append((level, alert_type, message))
        return True


@pytest.mark.unit
class TestBlockedExitAlert:
    def test_level_escalates_on_third_failure(self, quant_env, holding, clock, monkeypatch):
        from ai_stock.quant import exit_state, ops_monitor

        recorder = _Recorder()
        monkeypatch.setattr(ops_monitor, "get_alerts", lambda: recorder)

        for _ in range(3):
            exit_state.mark_and_notify(SYMBOL, reason="硬性止损", error="非交易时段")

        levels = [level for level, _, _ in recorder.events]
        # 前两次是可重试的 WARNING, 第三次起升级 ERROR (需要人工介入)
        assert levels == [ops_monitor.WARNING, ops_monitor.WARNING, ops_monitor.ERROR]
        assert {alert_type for _, alert_type, _ in recorder.events} == {f"sell_blocked:{SYMBOL}"}
        assert "累计失败 3 次" in recorder.events[-1][2]

    def test_alert_type_is_per_symbol(self, quant_env, holding, clock, monkeypatch):
        """固定 alert_type 会让第一只票的告警把其余票全部静默掉 —— 必须带标的。"""
        from ai_stock.quant import db_ops, exit_state, ops_monitor

        db_ops.upsert_holding({
            "symbol": "600000", "name": "浦发银行", "industry": "银行",
            "quantity": 1000, "available_quantity": 1000, "cost_price": 10.0,
        })
        manager = ops_monitor.AlertManager(silence_seconds=600)   # 静默窗口内只放第一只
        monkeypatch.setattr(ops_monitor, "get_alerts", lambda: manager)

        exit_state.mark_and_notify(SYMBOL, reason="硬性止损", error="非交易时段")
        exit_state.mark_and_notify("600000", reason="硬性止损", error="非交易时段")

        assert set(manager._last_emit) == {f"sell_blocked:{SYMBOL}", "sell_blocked:600000"}

    def test_queued_wording_differs_from_rejected(self, quant_env, holding, clock, monkeypatch):
        from ai_stock.quant import exit_state, ops_monitor

        recorder = _Recorder()
        monkeypatch.setattr(ops_monitor, "get_alerts", lambda: recorder)

        exit_state.mark_and_notify(SYMBOL, reason="硬性止损", error="跌停价 54.0 不可卖出", queued=True)

        assert "挂单未成交" in recorder.events[0][2]
        assert "被拒" not in recorder.events[0][2]


# ---------------------------------------------------------------------------
# 扫描入口: 冷却中不起 flow, 到期强制退出
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHoldScanRetry:
    @staticmethod
    def _scan(monkeypatch):
        from ai_stock.quant.agents import hold as hold_mod

        class _Orchestrator:
            def __init__(self):
                self.started: list[dict] = []

            def start_flow(self, flow_type, flow_id=None, data=None, **kwargs):
                self.started.append({"flow_type": flow_type, "flow_id": flow_id, "data": data})
                return flow_id

        orchestrator = _Orchestrator()
        monkeypatch.setattr(hold_mod, "get_orchestrator", lambda: orchestrator)
        return orchestrator

    def test_cooling_skips_flow(self, quant_env, holding, clock, monkeypatch):
        from ai_stock.quant import exit_state
        from ai_stock.quant.agents.hold import handle_hold_scan

        exit_state.mark_pending_exit(SYMBOL, reason="风控拒单", portion=1.0, error="跌停不可卖")
        orchestrator = self._scan(monkeypatch)

        out = handle_hold_scan({"payload": {"window": "202610201030"}})

        assert out["started_flows"] == []
        assert out["retry_cooling"] == [SYMBOL]
        assert orchestrator.started == []

    def test_ready_retry_injects_force_exit(self, quant_env, holding, clock, monkeypatch):
        from ai_stock.quant import exit_state
        from ai_stock.quant.agents.hold import handle_hold_scan
        from ai_stock.quant.config import SELL_RETRY_COOLDOWN_MINUTES

        exit_state.mark_pending_exit(SYMBOL, reason="风控拒单", portion=1.0, error="跌停不可卖")
        clock.tick(minutes=SELL_RETRY_COOLDOWN_MINUTES + 1)
        orchestrator = self._scan(monkeypatch)

        out = handle_hold_scan({"payload": {"window": "202610201130"}})

        assert out["started_flows"] == [SYMBOL]
        force = orchestrator.started[0]["data"]["force_exit"]
        assert force and force["reason"] == "风控拒单"

    def test_no_pending_starts_normal_flow(self, quant_env, holding, clock, monkeypatch):
        from ai_stock.quant.agents.hold import handle_hold_scan

        orchestrator = self._scan(monkeypatch)
        handle_hold_scan({"payload": {"window": "202610201030"}})

        assert orchestrator.started[0]["data"]["force_exit"] is None

    def test_confirm_needed_does_not_force_sell(self, quant_env, holding, clock, monkeypatch):
        """重大利空「待确认」不是「决定卖没卖掉」, 必须照常走决策。"""
        from ai_stock.quant import exit_state
        from ai_stock.quant.agents.hold import handle_hold_scan

        exit_state.mark_pending_exit(SYMBOL, reason="重大利空待确认", confirm_needed=1)
        orchestrator = self._scan(monkeypatch)

        out = handle_hold_scan({"payload": {"window": "202610201030"}})

        assert out["started_flows"] == [SYMBOL]
        assert orchestrator.started[0]["data"]["force_exit"] is None

    def test_switch_off_ignores_pending(self, quant_env, holding, clock, monkeypatch):
        from ai_stock.quant import db_ops, exit_state
        from ai_stock.quant.agents.hold import handle_hold_scan

        exit_state.mark_pending_exit(SYMBOL, reason="风控拒单", error="x")
        db_ops.set_config_value("sell_retry_enabled", "0")
        orchestrator = self._scan(monkeypatch)

        out = handle_hold_scan({"payload": {"window": "202610201030"}})

        assert out["started_flows"] == [SYMBOL]
        assert orchestrator.started[0]["data"]["force_exit"] is None


# ---------------------------------------------------------------------------
# 决策层: 退出意图跳过 LLM
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReduceForceExit:
    def test_force_exit_skips_llm_and_trend(self, quant_env, holding, fake_market):
        from ai_stock.quant.agents.hold import ReducePositionAgent

        agent = ReducePositionAgent(data_service=fake_market)
        called = []
        agent._check_major_bear = lambda *a, **k: called.append(1)

        out = agent.handle({"payload": {"context": {
            "symbol": SYMBOL,
            "trend_tracking": {"trend": "as_expected", "stop_loss_signal": False},
            "force_exit": {"reason": "风控拒单", "portion": 0.5, "fail_count": 2},
        }}})

        assert called == []                       # 没跑 LLM 利空判断
        assert out["action"] == "sell_partial"
        assert out["portion"] == 0.5
        assert "退出意图重试" in out["reason"]
        assert "2 次" in out["reason"]


# ---------------------------------------------------------------------------
# 执行层: 未成交 → 写意图; 成交 → 清意图
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExecuteSell:
    def test_risk_rejected_records_pending(self, quant_env, holding, fake_broker, fake_market, clock):
        from ai_stock.quant import db_ops, exit_state

        fake_broker.add_position(SYMBOL, 1000, cost=60.0, last_price=55.0)
        fake_market.quotes[SYMBOL] = _quote(55.0, 58.0)
        db_ops.set_config_value("global_trade_enable", "0")     # 任意风控拒单

        out = _agent(fake_market)._execute_sell(
            SYMBOL, _sell_context(), _reduce(), "t1", "",
        )

        assert out["status"] == "risk_rejected"
        pending = exit_state.get_pending_exit(db_ops.get_holding(SYMBOL))
        assert pending["fail_count"] == 1
        assert pending["portion"] == 1.0
        assert "全局交易开关" in pending["last_error"]
        assert out["pending_exit"]["reason"] == "硬性止损"

    def test_limit_down_queues_without_trade_log(
        self, quant_env, holding, fake_market, monkeypatch, clock,
    ):
        from ai_stock.quant import broker as broker_mod
        from ai_stock.quant import db_ops, exit_state

        sim = broker_mod.SimulatedBroker(quote_fn=lambda s: fake_market.quotes.get(s))
        sim._cash = 1_000_000.0
        sim._positions = {
            SYMBOL: {"qty": 1000, "available": 1000, "cost": 60.0, "buy_date": "2026-01-05"},
        }
        sim._today = broker_mod.SimulatedBroker._market_today()
        broker_mod.set_broker(sim)
        try:
            fake_market.quotes[SYMBOL] = _quote(54.0, 60.0, limit_down=54.0)

            out = _agent(fake_market)._execute_sell(
                SYMBOL, _sell_context(), _reduce(), "t1", "",
            )

            assert out["status"] == "pending"
            assert "跌停" in out["reason"]
            # 挂单不落 trade_log: 否则重试会吃掉单日委托笔数限额
            assert db_ops.count_orders_today() == 0
            assert db_ops.get_trades(limit=10) == []
            pending = exit_state.get_pending_exit(db_ops.get_holding(SYMBOL))
            assert pending["reason"] == "硬性止损"
        finally:
            broker_mod._broker = None

    def test_full_fill_removes_holding(self, quant_env, holding, fake_broker, fake_market, clock):
        from ai_stock.quant import db_ops, exit_state

        fake_broker.add_position(SYMBOL, 1000, cost=60.0, last_price=58.0)
        fake_broker.prices[SYMBOL] = 58.0
        fake_market.quotes[SYMBOL] = _quote(58.0, 58.0)
        exit_state.mark_pending_exit(SYMBOL, reason="上一轮风控拒单", error="x")

        out = _agent(fake_market)._execute_sell(
            SYMBOL, _sell_context(), _reduce(), "t1", "",
        )

        assert out["status"] == "filled"
        assert out["cleared"] is True
        assert db_ops.get_holding(SYMBOL) is None

    def test_partial_fill_clears_pending(self, quant_env, holding, fake_broker, fake_market, clock):
        """减仓一半成交后, 退出意图必须清掉, 否则下一轮会再卖一半。"""
        from ai_stock.quant import db_ops, exit_state

        fake_broker.add_position(SYMBOL, 1000, cost=60.0, last_price=58.0)
        fake_broker.prices[SYMBOL] = 58.0
        fake_market.quotes[SYMBOL] = _quote(58.0, 58.0)
        exit_state.mark_pending_exit(SYMBOL, reason="上一轮风控拒单", error="x")

        out = _agent(fake_market)._execute_sell(
            SYMBOL, _sell_context(), _reduce(portion=0.5), "t1", "",
        )

        assert out["status"] == "filled"
        assert out["quantity"] == 500
        assert db_ops.get_holding(SYMBOL)["quantity"] == 500
        assert exit_state.get_pending_exit(db_ops.get_holding(SYMBOL)) is None

    def test_switch_off_keeps_old_behaviour(self, quant_env, holding, fake_broker, fake_market, clock):
        from ai_stock.quant import db_ops, exit_state

        fake_broker.add_position(SYMBOL, 1000, cost=60.0, last_price=55.0)
        fake_market.quotes[SYMBOL] = _quote(55.0, 58.0)
        db_ops.set_config_value("global_trade_enable", "0")
        db_ops.set_config_value("sell_retry_enabled", "0")

        out = _agent(fake_market)._execute_sell(
            SYMBOL, _sell_context(), _reduce(), "t1", "",
        )

        assert out["status"] == "risk_rejected"
        assert out["pending_exit"] is None
        assert exit_state.get_pending_exit(db_ops.get_holding(SYMBOL)) is None


# ---------------------------------------------------------------------------
# 跌停: 风控放行 + 可回滚
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLimitDownSellGate:
    def _sell_order(self):
        from ai_stock.quant.broker import OrderRequest, OrderSide, OrderType

        return OrderRequest(symbol=SYMBOL, side=OrderSide.SELL, quantity=1000,
                            order_type=OrderType.MARKET)

    def test_limit_down_sell_passes_by_default(
        self, quant_env, holding, fake_broker, fake_market,
    ):
        from ai_stock.quant.risk_control import pre_trade_check

        fake_market.quotes[SYMBOL] = _quote(54.0, 60.0, limit_down=54.0)
        check = pre_trade_check(self._sell_order())

        assert check.ok, check.reason

    def test_rollback_key_restores_rejection(
        self, quant_env, holding, fake_broker, fake_market,
    ):
        from ai_stock.quant import db_ops
        from ai_stock.quant.risk_control import pre_trade_check

        db_ops.set_config_value("allow_limit_down_sell", "0")
        fake_market.quotes[SYMBOL] = _quote(54.0, 60.0, limit_down=54.0)
        check = pre_trade_check(self._sell_order())

        assert not check.ok
        assert "跌停价" in check.reason


# ---------------------------------------------------------------------------
# 强制减仓被拒同样留痕
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_force_reduce_rejection_records_pending(
    quant_env, holding, fake_broker, fake_market, clock,
):
    from ai_stock.quant import db_ops, exit_state
    from ai_stock.quant.agents.risk import handle_force_reduce

    fake_broker.cash = 200_000.0
    fake_broker.add_position(SYMBOL, 1000, cost=60.0, last_price=60.0)
    fake_market.quotes[SYMBOL] = _quote(60.0, 60.0)
    db_ops.set_config_value("global_trade_enable", "0")

    out = handle_force_reduce({
        "task_id": "t1",
        "payload": {"symbol": SYMBOL, "reason": "单只仓位占比超限", "target_pct": 0.1},
    })

    assert out["status"] == "risk_rejected"
    pending = exit_state.get_pending_exit(db_ops.get_holding(SYMBOL))
    assert pending is not None
    assert 0 < pending["portion"] <= 1.0
    assert "强制减仓" in pending["reason"]


# ---------------------------------------------------------------------------
# FR-06: 回测与实盘同口径 (最低价判破位)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBacktestStopPolicyWithLows:
    def test_low_breach_triggers_stop_loss(self):
        """收盘价没破, 但当日最低价破了 → 必须触发止损 (实盘就是盘中卖掉的)。"""
        from ai_stock.quant.evolution import backtest_stop_policy

        closes = [100.0, 100.0, 100.0, 99.0, 100.0]
        lows = [100.0, 100.0, 100.0, 94.0, 100.0]

        close_only = backtest_stop_policy(closes, stop_loss_pct=0.05)
        with_lows = backtest_stop_policy(closes, stop_loss_pct=0.05, lows=lows)

        assert close_only["exit_reason"] != "stop_loss"
        assert with_lows["exit_reason"] == "stop_loss"
        assert with_lows["exit_price_source"] == "low"
        assert with_lows["holding_bars"] == 3
        # 收盘收回止损位之上 → 按止损价成交, 而不是按当天收盘价
        assert with_lows["total_return"] == pytest.approx(-0.05, abs=1e-9)

    def test_gap_down_fills_at_worse_close(self):
        from ai_stock.quant.evolution import backtest_stop_policy

        closes = [100.0, 90.0]
        lows = [100.0, 88.0]
        out = backtest_stop_policy(closes, stop_loss_pct=0.05, lows=lows)

        assert out["exit_reason"] == "stop_loss"
        assert out["total_return"] == pytest.approx(-0.10, abs=1e-9)

    def test_length_mismatch_falls_back_to_close(self):
        from ai_stock.quant.evolution import backtest_stop_policy

        out = backtest_stop_policy([100.0, 99.0], stop_loss_pct=0.05, lows=[94.0])
        assert out["exit_price_source"] == "close"
        assert out["exit_reason"] != "stop_loss"

    def test_segment_results_accepts_plain_lists(self):
        """既有调用传的是裸收盘价列表, 必须继续可用。"""
        from ai_stock.quant.evolution import _segment_results

        results = _segment_results({"600000": [100.0, 95.0, 94.0, 93.0, 92.0]}, range(0, 5), {})
        assert len(results) == 1
        assert results[0]["exit_reason"] == "stop_loss"

    def test_collect_series_carries_low(self, quant_env, fake_market, monkeypatch):
        from ai_stock.quant import data_service, evolution

        monkeypatch.setattr(data_service, "get_data_service", lambda: fake_market)
        series = evolution._collect_series(["600000"], lookback_days=120)

        assert len(series["600000"]) == fake_market.bars
        assert series["600000"].low[0] == pytest.approx(9.8)
