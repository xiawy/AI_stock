"""账户度量回归测试 (FR-01 回撤基准 / FR-03 单日盈亏含浮亏)。

两个错误的共同后果是「冻结闸门形同虚设」:

- FR-01 用 ``max(初始资金, 当前总资产)`` 当峰值 —— 盈利态下它恒等于当前总资产,
  回撤永远是 0。账户涨到 150 万再跌回 120 万 (回撤 20%) 时系统认为一切正常。
- FR-03 只用「当日已实现盈亏」判单日亏损 —— 持仓跌 6% 但没卖出, 当日盈亏是 0,
  单日亏损阈值同样永不触发。

修复后: 回撤以持久化的历史净值高点为基准, 单日亏损 = 已实现 + 持仓浮动盈亏。
"""

import pytest


TODAY = "2026-10-20"          # 与 conftest._pin_trading_clock 钉住的日期一致


def _buy_order(symbol="600000", qty=1000):
    from ai_stock.quant.broker import OrderRequest, OrderSide, OrderType

    return OrderRequest(
        symbol=symbol, side=OrderSide.BUY, quantity=qty, order_type=OrderType.MARKET,
    )


def _quote(price, prev_close):
    return {
        "price": price, "prev_close": prev_close, "name": "测试股",
        "limit_up": round(prev_close * 1.1, 2), "limit_down": round(prev_close * 0.9, 2),
    }


# ---------------------------------------------------------------------------
# FR-01: 回撤基准 = 历史净值高点
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDrawdownBasis:
    def test_first_snapshot_initialises_hwm_without_fake_drawdown(
        self, quant_env, fake_broker, fake_market,
    ):
        from ai_stock.quant import db_ops, risk_control

        fake_broker.cash = 1_000_000.0
        snap = risk_control.account_snapshot()

        assert float(db_ops.get_config_value("account_hwm")) == pytest.approx(1_000_000)
        assert float(db_ops.get_config_value("account_hwm_initial")) == pytest.approx(1_000_000)
        assert snap["drawdown_pct"] == 0.0
        assert snap["drawdown_basis"] == "hwm"

    def test_drawdown_measured_against_hwm_not_current_assets(
        self, quant_env, fake_broker, fake_market,
    ):
        """150 万 → 120 万: 旧口径回撤 0, 新口径 20% 且触发冻结。"""
        from ai_stock.quant import db_ops, risk_control

        fake_broker.cash = 1_500_000.0
        risk_control.account_snapshot()
        assert float(db_ops.get_config_value("account_hwm")) == pytest.approx(1_500_000)

        fake_broker.cash = 1_200_000.0
        snap = risk_control.account_snapshot()
        assert snap["drawdown_pct"] == pytest.approx(0.20, abs=1e-4)

        db_ops.set_config_value("max_drawdown_pct", "0.10")   # 生产口径
        fake_market.quotes["600000"] = _quote(10.0, 10.0)
        result = risk_control.pre_trade_check(_buy_order())

        assert not result.ok
        assert "总回撤" in result.reason
        assert db_ops.get_config_value("trade_frozen") == "1"

    def test_shallow_drawdown_does_not_block_buying(
        self, quant_env, fake_broker, fake_market,
    ):
        """150 万 → 146 万 (2.67%): 低于 10% 阈值, 买入必须通过。"""
        from ai_stock.quant import risk_control

        fake_broker.cash = 1_500_000.0
        risk_control.account_snapshot()

        fake_broker.cash = 1_460_000.0
        snap = risk_control.account_snapshot()
        assert snap["drawdown_pct"] == pytest.approx(0.0267, abs=1e-4)

        fake_market.quotes["600000"] = _quote(10.0, 10.0)
        result = risk_control.pre_trade_check(_buy_order())
        assert result.ok, result.reason

    def test_initial_cash_change_rebases_hwm(self, quant_env, fake_broker, fake_market):
        """账户重置 (初始资金 100 万 → 50 万): 不得拿旧高点制造伪回撤。"""
        from ai_stock.quant import db_ops, risk_control

        fake_broker.cash = 1_500_000.0
        risk_control.account_snapshot()

        db_ops.set_config_value("initial_cash", "500000")
        fake_broker.cash = 500_000.0
        snap = risk_control.account_snapshot()

        assert snap["drawdown_pct"] == 0.0
        assert float(db_ops.get_config_value("account_hwm")) == pytest.approx(500_000)
        assert float(db_ops.get_config_value("account_hwm_initial")) == pytest.approx(500_000)

    def test_rebase_takes_current_assets_when_above_new_initial(
        self, quant_env, fake_broker, fake_market,
    ):
        """重置后实际资产高于新初始资金时, 以资产起算 (不制造负回撤)。"""
        from ai_stock.quant import db_ops, risk_control

        db_ops.set_config_value("initial_cash", "500000")
        fake_broker.cash = 800_000.0
        snap = risk_control.account_snapshot()

        assert snap["drawdown_pct"] == 0.0
        assert float(db_ops.get_config_value("account_hwm")) == pytest.approx(800_000)

    def test_account_failure_keeps_hwm_intact(self, quant_env, fake_broker, fake_market):
        """取数失败回零, 但绝不能把 0 写进高点。"""
        from ai_stock.quant import db_ops, risk_control

        fake_broker.cash = 1_200_000.0
        risk_control.account_snapshot()

        fake_broker.fail_account = True
        snap = risk_control.account_snapshot()

        assert snap["drawdown_basis"] == "unavailable"
        assert snap["drawdown_pct"] == 0.0
        assert float(db_ops.get_config_value("account_hwm")) == pytest.approx(1_200_000)

    def test_basis_rollback_switch_restores_legacy_behaviour(
        self, quant_env, fake_broker, fake_market,
    ):
        """drawdown_basis=initial → 回到 max(初始资金, 当前资产) 的旧口径。"""
        from ai_stock.quant import db_ops, risk_control

        fake_broker.cash = 1_500_000.0
        risk_control.account_snapshot()

        db_ops.set_config_value("drawdown_basis", "initial")
        fake_broker.cash = 1_200_000.0
        snap = risk_control.account_snapshot()

        assert snap["drawdown_basis"] == "initial"
        assert snap["drawdown_pct"] == 0.0     # 1.2M 低于初始资金 → 旧口径不回撤


# ---------------------------------------------------------------------------
# FR-03: 当日盈亏 = 已实现 + 持仓浮动盈亏
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDailyPnlIncludesUnrealized:
    def test_holding_drop_counts_into_daily_pnl(self, quant_env, fake_broker, fake_market):
        """持仓 60 万当日跌 6% (未卖出) → 单日亏损 3.6 万, 超 3 万阈值冻结。"""
        from ai_stock.quant import db_ops, risk_control

        fake_broker.cash = 400_000.0
        fake_broker.add_position("600519", 10_000, cost=60.0, last_price=56.4)
        # 昨收 60 → 现价 56.4, 隔日持仓以昨收为基准
        fake_market.quotes["600519"] = _quote(56.4, 60.0)

        snap = risk_control.account_snapshot()

        assert snap["daily_pnl"] == 0.0                              # 已实现为 0
        assert snap["daily_pnl_total"] == pytest.approx(-36_000, abs=1.0)
        assert snap["mtm_incomplete"] == []

        fake_market.quotes["600000"] = _quote(10.0, 10.0)
        result = risk_control.pre_trade_check(_buy_order())

        assert not result.ok
        assert "单日亏损" in result.reason
        assert db_ops.get_config_value("trade_frozen") == "1"

    def test_gain_does_not_freeze(self, quant_env, fake_broker, fake_market):
        from ai_stock.quant import risk_control

        fake_broker.cash = 400_000.0
        fake_broker.add_position("600519", 10_000, cost=60.0, last_price=62.0)
        fake_market.quotes["600519"] = _quote(62.0, 60.0)

        snap = risk_control.account_snapshot()
        assert snap["daily_pnl_total"] == pytest.approx(20_000, abs=1.0)

    def test_position_bought_today_uses_cost_as_basis(
        self, quant_env, fake_broker, fake_market,
    ):
        """当日买入的持仓: 当日盈亏从建仓价起算, 不能混入此前涨幅。"""
        from ai_stock.quant import risk_control

        fake_broker.cash = 400_000.0
        fake_broker.add_position(
            "600519", 10_000, cost=56.0, last_price=56.4, buy_date=TODAY,
        )
        fake_market.quotes["600519"] = _quote(56.4, 50.0)

        snap = risk_control.account_snapshot()
        # (56.4 - 56.0) × 10000 = 4000, 而不是按昨收 50 算出的 64000
        assert snap["daily_pnl_total"] == pytest.approx(4_000, abs=1.0)

    def test_mtm_counts_t1_frozen_quantity(self, quant_env, fake_broker, fake_market):
        """当日买入的持仓 ``quantity`` (可用量) 为 0, 浮亏必须按 total_quantity 计。"""
        from ai_stock.quant import risk_control

        fake_broker.cash = 400_000.0
        pos = fake_broker.add_position(
            "600519", 2_000, cost=60.0, last_price=57.0, buy_date=TODAY,
        )
        pos.quantity = 0          # T+1 冻结, 当日不可卖
        fake_market.quotes["600519"] = _quote(57.0, 58.0)

        snap = risk_control.account_snapshot()
        assert snap["daily_pnl_total"] == pytest.approx(-6_000, abs=1.0)

    def test_missing_quote_reported_not_silently_zero(
        self, quant_env, fake_broker, fake_market,
    ):
        """行情拿不到的标的要显式列出来, 而不是当作不亏不赚。"""
        from ai_stock.quant import risk_control

        fake_broker.cash = 400_000.0
        fake_broker.add_position("600519", 10_000, cost=60.0, last_price=0.0)
        # 不配置 quote → 价格与昨收都取不到
        snap = risk_control.account_snapshot()

        assert snap["mtm_incomplete"] == ["600519"]
        assert snap["daily_pnl_total"] == 0.0

    def test_legacy_snapshot_without_total_field_still_works(
        self, quant_env, fake_broker, fake_market,
    ):
        """外部传入的旧结构 snapshot 只有 daily_pnl, 判据回退而不是 KeyError。"""
        from ai_stock.quant import db_ops, risk_control

        snap = {
            "available_cash": 1_000_000.0, "market_value": 0.0,
            "total_assets": 1_000_000.0, "drawdown_pct": 0.0,
            "daily_pnl": -50_000.0,
        }
        fake_market.quotes["600000"] = _quote(10.0, 10.0)
        result = risk_control.pre_trade_check(_buy_order(), snap)

        assert not result.ok
        assert "单日亏损" in result.reason
        assert db_ops.get_config_value("trade_frozen") == "1"


# ---------------------------------------------------------------------------
# 异步风控扫描同口径
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_async_risk_scan_freezes_on_unrealized_loss(quant_env, fake_broker, fake_market):
    from ai_stock.quant import db_ops, risk_control

    fake_broker.cash = 400_000.0
    fake_broker.add_position("600519", 10_000, cost=60.0, last_price=56.4)
    fake_market.quotes["600519"] = _quote(56.4, 60.0)
    db_ops.upsert_holding({
        "symbol": "600519", "name": "贵州茅台", "quantity": 10_000,
        "available_quantity": 10_000, "cost_price": 60.0,
    })

    out = risk_control.async_risk_scan()

    issues = {f["issue"] for f in out["findings"]}
    assert "daily_loss_freeze" in issues
    assert db_ops.get_config_value("trade_frozen") == "1"
