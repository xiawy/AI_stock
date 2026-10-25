"""持仓决策口径回归测试 (FR-05 止盈与多档移动止损 / FR-07 利空两段式).

修复前的三个缺口:

- **止盈只记录不执行**: ``take_profit`` 存进持仓后从没有代码拿它对照价格, 等于
  没有止盈。
- **移动止损只有一档**: 浮盈 3%~15% 区间完全裸奔, 赚 10% 回吐到成本以下才动。
- **利空单次判定即清仓**: LLM 一次采样波动 (标题党旧闻) 就能清掉刚建仓的仓位;
  反过来, 真利空第一天大跌也要白等一轮 —— 所以两段式配一个跌幅旁路。
"""

import pytest


SYMBOL = "600519"
COST = 100.0


def _holding(quant_env, **overrides):
    from ai_stock.quant import db_ops

    item = {
        "symbol": SYMBOL, "name": "贵州茅台", "industry": "白酒",
        "quantity": 1000, "available_quantity": 1000, "cost_price": COST,
        "stop_loss": 95.0, "take_profit": 120.0,
    }
    item.update(overrides)
    db_ops.upsert_holding(item)
    return item


def _indicators(close, high=None):
    high = high if high is not None else close * 1.01
    n = 20
    return {
        "close_series": [close] * n,
        "high_series": [high] * n,
        "low_series": [close * 0.99] * n,
        "atr": 0.0,
        "atr_pct": 0.0,
    }


def _quote(price, prev_close=None, change_pct=None):
    return {
        "price": price, "prev_close": prev_close or price, "name": "贵州茅台",
        "change_pct": change_pct,
        "limit_up": round((prev_close or price) * 1.1, 2),
        "limit_down": round((prev_close or price) * 0.9, 2),
    }


# ---------------------------------------------------------------------------
# FR-05: 多档移动止损
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTrailingStopTiers:
    def _run(self, quant_env, fake_market, fake_broker, *, gain, stop_loss=95.0):
        from ai_stock.quant.agents.hold import AddPositionAgent

        price = COST * (1 + gain)
        fake_market.indicators[SYMBOL] = _indicators(price)
        fake_market.quotes[SYMBOL] = _quote(price)
        agent = AddPositionAgent(data_service=fake_market)
        agent._check_new_catalyst = lambda *a, **k: False

        out = agent.handle({"payload": {"context": {
            "symbol": SYMBOL, "cost_price": COST, "quantity": 1000,
            "stop_loss": stop_loss,
            "trend_tracking": {
                "gain_ratio": gain, "trend": "as_expected",
                "stop_loss_signal": False, "time_stop_signal": False,
            },
        }}})
        return out

    @pytest.mark.parametrize("gain,expected", [
        (0.04, None),                    # 未到档: 不动
        (0.07, 100.0),                   # 6% → 保本
        (0.13, 106.0),                   # 12% → 成本 +6%
        (0.25, 112.0),                   # 20% → 成本 +12%
    ])
    def test_tiers(self, quant_env, fake_market, fake_broker, gain, expected):
        _holding(quant_env)
        out = self._run(quant_env, fake_market, fake_broker, gain=gain)
        assert out["new_stop_loss"] == expected

    def test_never_lowers_existing_stop(self, quant_env, fake_market, fake_broker):
        """浮盈回落时不能把已上移的止损位往下调。"""
        _holding(quant_env, stop_loss=104.0)
        out = self._run(quant_env, fake_market, fake_broker, gain=0.07, stop_loss=104.0)
        assert out["new_stop_loss"] is None

    def test_rollback_uses_single_tier(self, quant_env, fake_market, fake_broker):
        """trailing_tiers_enabled=0 → 回到「15% 才上移到 +10%」的旧口径。"""
        from ai_stock.quant import db_ops

        _holding(quant_env)
        db_ops.set_config_value("trailing_tiers_enabled", "0")

        assert self._run(quant_env, fake_market, fake_broker, gain=0.07)["new_stop_loss"] is None
        assert self._run(quant_env, fake_market, fake_broker, gain=0.16)["new_stop_loss"] == 110.0


# ---------------------------------------------------------------------------
# FR-05: 止盈减半
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTakeProfit:
    def _run(self, quant_env, fake_market, *, price, take_profit=120.0, trend="as_expected"):
        from ai_stock.quant.agents.hold import ReducePositionAgent

        agent = ReducePositionAgent(data_service=fake_market)
        agent._check_major_bear = lambda *a, **k: None          # 无利空
        return agent.handle({"payload": {"context": {
            "symbol": SYMBOL, "cost_price": COST, "quantity": 1000,
            "take_profit": take_profit,
            "trend_tracking": {
                "trend": trend, "current_close": price,
                "stop_loss_signal": False, "time_stop_signal": False,
                "loss_ratio": 0.0, "gain_ratio": (price - COST) / COST,
            },
        }}})

    def test_touch_take_profit_sells_half(self, quant_env, fake_market):
        out = self._run(quant_env, fake_market, price=121.0)
        assert out["action"] == "sell_partial"
        assert out["portion"] == 0.5
        assert "止盈价" in out["reason"]

    def test_below_take_profit_holds(self, quant_env, fake_market):
        out = self._run(quant_env, fake_market, price=115.0)
        assert out["action"] == "hold"

    def test_rollback_disables_take_profit(self, quant_env, fake_market):
        from ai_stock.quant import db_ops

        db_ops.set_config_value("take_profit_partial_enabled", "0")
        assert self._run(quant_env, fake_market, price=121.0)["action"] == "hold"

    def test_stop_loss_wins_over_take_profit(self, quant_env, fake_market):
        """止盈价与止损信号同时成立时, 止损优先 (不能只减半仓留一半裸奔)。"""
        from ai_stock.quant.agents.hold import ReducePositionAgent

        agent = ReducePositionAgent(data_service=fake_market)
        agent._check_major_bear = lambda *a, **k: None
        out = agent.handle({"payload": {"context": {
            "symbol": SYMBOL, "cost_price": COST, "quantity": 1000,
            "take_profit": 120.0,
            "trend_tracking": {
                "trend": "below", "current_close": 121.0,
                "stop_loss_signal": True, "time_stop_signal": False,
                "loss_ratio": 0.06, "dynamic_stop_price": 94.0,
            },
        }}})
        assert out["action"] == "sell_all"
        assert "硬性止损" in out["reason"]


# ---------------------------------------------------------------------------
# FR-07: 重大利空两段式
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMajorBearTwoStage:
    def _run(self, quant_env, fake_market, *, bear, change_pct=None, trend="as_expected"):
        from ai_stock.quant.agents.hold import MajorBearCheck, ReducePositionAgent

        agent = ReducePositionAgent(data_service=fake_market)
        agent._check_major_bear = lambda *a, **k: (
            MajorBearCheck(has_major_bear=bear, reason="监管处罚" if bear else "")
        )
        return agent.handle({"payload": {"context": {
            "symbol": SYMBOL, "cost_price": COST, "quantity": 1000,
            "take_profit": 0.0,
            "trend_tracking": {
                "trend": trend, "current_close": 100.0,
                "stop_loss_signal": False, "time_stop_signal": False,
                "loss_ratio": 0.0, "daily_change_ratio": (
                    None if change_pct is None else change_pct / 100.0
                ),
            },
        }}})

    def test_first_judgement_waits(self, quant_env, fake_market):
        from ai_stock.quant import db_ops, exit_state

        _holding(quant_env)
        out = self._run(quant_env, fake_market, bear=True)

        assert out["action"] == "hold"
        assert out["portion"] == 0.0
        pending = exit_state.get_pending_exit(db_ops.get_holding(SYMBOL))
        assert pending["confirm_needed"] == 1
        assert "监管处罚" in pending["reason"]

    def test_second_judgement_sells(self, quant_env, fake_market):
        _holding(quant_env)
        self._run(quant_env, fake_market, bear=True)          # 第一轮: 登记待确认

        out = self._run(quant_env, fake_market, bear=True)     # 第二轮: 再次判定为真

        assert out["action"] == "sell_all"
        assert out["portion"] == 1.0
        assert "二次确认" in out["reason"]

    def test_big_drop_bypasses_waiting(self, quant_env, fake_market):
        """利空 + 当日跌 3.2% → 首轮即清仓, 不白亏一天。"""
        _holding(quant_env)
        out = self._run(quant_env, fake_market, bear=True, change_pct=-3.2)

        assert out["action"] == "sell_all"
        assert "当日跌幅" in out["reason"]

    def test_small_drop_still_waits(self, quant_env, fake_market):
        _holding(quant_env)
        out = self._run(quant_env, fake_market, bear=True, change_pct=-1.5)
        assert out["action"] == "hold"

    def test_rollback_single_judgement(self, quant_env, fake_market):
        from ai_stock.quant import db_ops

        _holding(quant_env)
        db_ops.set_config_value("major_bear_confirm_rounds", "0")

        out = self._run(quant_env, fake_market, bear=True)

        assert out["action"] == "sell_all"
        assert "直接清仓" in out["reason"]

    def test_no_bear_keeps_holding(self, quant_env, fake_market):
        _holding(quant_env)
        assert self._run(quant_env, fake_market, bear=False)["action"] == "hold"

    def test_bear_with_below_trend_still_waits(self, quant_env, fake_market):
        """利空待确认期间, 「走势不及预期」也不该抢先减半 (否则两段式形同虚设)。"""
        _holding(quant_env)
        out = self._run(quant_env, fake_market, bear=True, trend="below")
        assert out["action"] == "hold"
