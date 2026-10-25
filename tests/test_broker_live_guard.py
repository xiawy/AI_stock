"""实盘适配器风控收口回归测试 (FR-09).

修复前: ``HuaxingBrokerAdapter`` 只做 4 项权限检查 (全局开关/笔数/金额/白名单),
业务层的 ``pre_trade_check`` 一旦被绕过 (脚本直连、调试下单、新增调用点漏改),
回撤冻结、持仓上限、行业集中度这些约束在实盘全部失效。

修复后: broker 层再走一遍 ``pre_trade_check`` (纵深防御), 并在装配时要求实盘
显式打开 ``global_trade_enable``。
"""

import pytest


SYMBOL = "600000"


class _FakeLiveClient:
    """华兴 SDK 的最小替身: 只实现适配器用到的调用."""

    def __init__(self):
        self.placed: list[dict] = []

    def login(self):
        return True

    def logout(self):
        return None

    def get_account(self):
        return {
            "total_assets": 1_000_000.0, "available_cash": 1_000_000.0,
            "frozen_cash": 0.0, "market_value": 0.0,
        }

    def get_positions(self):
        return []

    def place_order(self, **kwargs):
        self.placed.append(kwargs)
        return "LIVE-1"

    def query_order(self, order_id):
        return {"status": "pending", "filled_quantity": 0, "avg_price": 0.0}

    def cancel_order(self, order_id):
        return True


def _live_broker():
    from ai_stock.quant.broker import HuaxingBrokerAdapter

    adapter = HuaxingBrokerAdapter(_FakeLiveClient())
    adapter._connected = True
    return adapter


def _buy_order(quantity=1000, used_ratio=None):
    from ai_stock.quant.broker import OrderRequest, OrderSide, OrderType

    extra = None if used_ratio is None else {"used_ratio": used_ratio}
    return OrderRequest(
        symbol=SYMBOL, side=OrderSide.BUY, quantity=quantity,
        order_type=OrderType.MARKET, extra=extra,
    )


def _quote(price=10.0):
    return {
        "price": price, "prev_close": price, "name": "浦发银行",
        "limit_up": round(price * 1.1, 2), "limit_down": round(price * 0.9, 2),
    }


# ---------------------------------------------------------------------------
# 装配自检
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBrokerAssembly:
    def test_live_broker_requires_explicit_switch(self, quant_env):
        from ai_stock.quant import broker as broker_mod
        from ai_stock.quant.broker import OrderRejectException

        broker_mod._broker = None
        try:
            with pytest.raises(OrderRejectException, match="global_trade_enable"):
                broker_mod.set_broker(_live_broker())
            assert broker_mod._broker is None      # 装配失败必须保持未装配
        finally:
            broker_mod._broker = None

    def test_live_broker_assembles_when_switch_on(self, quant_env):
        from ai_stock.quant import broker as broker_mod, db_ops

        db_ops.set_config_value("global_trade_enable", "1")
        broker_mod._broker = None
        try:
            broker_mod.set_broker(_live_broker())
            assert broker_mod.get_broker().broker_name == "huaxing"
        finally:
            broker_mod._broker = None

    def test_paper_broker_assembles_without_switch(self, quant_env):
        """模拟盘不受实盘开关约束 (否则本地跑不动)。"""
        from ai_stock.quant import broker as broker_mod

        broker_mod._broker = None
        try:
            broker_mod.set_broker(broker_mod.SimulatedBroker())
            assert broker_mod.get_broker().broker_name == "simulated"
        finally:
            broker_mod._broker = None


# ---------------------------------------------------------------------------
# 下单前复核
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLivePreTradeGuard:
    def _assemble(self, quant_env, fake_market):
        from ai_stock.quant import broker as broker_mod, db_ops

        db_ops.set_config_value("global_trade_enable", "1")
        fake_market.quotes[SYMBOL] = _quote()
        adapter = _live_broker()
        broker_mod._broker = adapter
        return adapter

    def test_frozen_account_blocks_live_order(self, quant_env, fake_market, monkeypatch):
        from ai_stock.quant import broker as broker_mod, db_ops
        from ai_stock.quant.broker import OrderRejectException

        adapter = self._assemble(quant_env, fake_market)
        db_ops.set_config_value("trade_frozen", "1")
        try:
            with pytest.raises(OrderRejectException, match="实盘风控复核拒绝"):
                adapter.place_order(_buy_order())
            assert adapter._client.placed == []          # 一张真单都没发出去
        finally:
            broker_mod._broker = None

    def test_high_confidence_ratio_is_not_double_rejected(self, quant_env, fake_market):
        """24% 的合法建仓单: 透传 used_ratio 后必须放行 (否则 P1-1 的坑重演)。"""
        from ai_stock.quant import broker as broker_mod

        adapter = self._assemble(quant_env, fake_market)
        try:
            result = adapter.place_order(_buy_order(quantity=24_000, used_ratio=0.24))
            assert len(adapter._client.placed) == 1
            assert result.status.value in ("pending", "filled", "unknown")
        finally:
            broker_mod._broker = None

    def test_missing_ratio_falls_back_to_base_ratio(self, quant_env, fake_market):
        """不透传时按基础 20% 复核 → 24% 的单被拒 (行为可预期, 有明确报错)。"""
        from ai_stock.quant import broker as broker_mod
        from ai_stock.quant.broker import OrderRejectException

        adapter = self._assemble(quant_env, fake_market)
        try:
            with pytest.raises(OrderRejectException, match="超过可用资金"):
                adapter.place_order(_buy_order(quantity=24_000))
            assert adapter._client.placed == []
        finally:
            broker_mod._broker = None

    def test_guard_switch_off_restores_old_permission_only(
        self, quant_env, fake_market,
    ):
        from ai_stock.quant import broker as broker_mod, db_ops

        adapter = self._assemble(quant_env, fake_market)
        db_ops.set_config_value("trade_frozen", "1")
        db_ops.set_config_value("live_pre_trade_guard", "0")
        try:
            result = adapter.place_order(_buy_order())
            assert len(adapter._client.placed) == 1     # 只剩 4 项权限检查
            assert result.status.value == "pending"
        finally:
            broker_mod._broker = None

    def test_sell_order_is_also_guarded(self, quant_env, fake_market):
        """闸门买卖同开: 冻结账户的卖单在 broker 层也应被复核 (由业务层放行)。"""
        from ai_stock.quant import broker as broker_mod, db_ops
        from ai_stock.quant.broker import OrderRejectException, OrderRequest, OrderSide

        adapter = self._assemble(quant_env, fake_market)
        db_ops.set_config_value("global_trade_enable", "0")
        try:
            with pytest.raises(OrderRejectException, match="实盘全局开关关闭"):
                adapter.place_order(OrderRequest(
                    symbol=SYMBOL, side=OrderSide.SELL, quantity=1000,
                ))
        finally:
            broker_mod._broker = None
