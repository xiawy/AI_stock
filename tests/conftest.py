"""Shared pytest fixtures that prevent CI hangs when API keys are absent."""

import os
from unittest.mock import MagicMock, patch

import pytest


def pytest_configure(config):
    for marker in ("unit", "integration", "smoke"):
        config.addinivalue_line("markers", f"{marker}: {marker}-level tests")


_API_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "ZHIPU_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
)


@pytest.fixture(autouse=True)
def _dummy_api_keys(monkeypatch):
    for env_var in _API_KEY_ENV_VARS:
        monkeypatch.setenv(env_var, os.environ.get(env_var, "placeholder"))


@pytest.fixture(autouse=True)
def _hermetic_dataflow_state(monkeypatch):
    """隔离 a_stock 里会跨用例/跨进程泄漏的全局态, 保证测试离线且互不污染.

    - mootdx 负缓存落盘(H1): 不隔离的话, 某用例写下的"不可用截止时间戳"会被后续
      用例 _load 进来, 使 _get_mootdx_client 跳过探测直接抛错, 选服务器用例全挂,
      还会写脏用户真实 cache_dir. 这里把路径置 None → 退化为纯内存负缓存.
      (专门验证落盘行为的用例自行把 _mootdx_negcache_path 覆盖到 tmp_path.)
    - 同花顺一致预期"连续落空"计数(H2): 一次性全局态, 逐用例复位防串味.
    """
    from ai_stock.dataflows import a_stock

    monkeypatch.setattr(a_stock, "_mootdx_negcache_path", lambda: None)
    monkeypatch.setattr(a_stock, "_mootdx_negcache_loaded", [False])
    monkeypatch.setattr(a_stock, "_ths_miss_streak", [0])
    monkeypatch.setattr(a_stock, "_ths_block_warned", [False])


@pytest.fixture(autouse=True)
def _pin_trading_clock(monkeypatch):
    """把风控的"当前市场时间"钉在交易日盘中 (2026-10-20 10:30 +8)。

    2026-10-20 新增的「交易时段闸门」会让 ``pre_trade_check`` 在夜间/周末一律
    拒单，风控用例因此在 CI 上会随时段全红。这里统一钉住时钟；生产路径不走
    这个 fixture。需要验证「盘后拒单」的用例自行再 monkeypatch 覆盖即可。
    """
    from datetime import datetime, timedelta, timezone

    from ai_stock.quant import risk_control

    pinned = datetime(2026, 10, 20, 10, 30, tzinfo=timezone(timedelta(hours=8)))
    monkeypatch.setattr(risk_control, "_now_market", lambda: pinned)


@pytest.fixture()
def mock_llm_client():
    client = MagicMock()
    client.get_llm.return_value = MagicMock()
    with patch(
        "ai_stock.llm_clients.factory.create_llm_client",
        return_value=client,
    ):
        yield client


# ---------------------------------------------------------------------------
# 量化链路公共夹具 (风控 / 卖出 / 实盘守卫用例共用)
# ---------------------------------------------------------------------------


@pytest.fixture()
def quant_env(tmp_path, monkeypatch):
    """独立临时 SQLite + SQLite 队列后端, 不碰真实库与 Redis."""
    from ai_stock.quant import db, mq

    db_url = f"sqlite:///{tmp_path / 'quant_test.db'}"
    monkeypatch.setenv("QUANT_DB_URL", db_url)
    monkeypatch.setattr(mq, "MQ_BACKEND", "sqlite")
    db.reset_engine(db_url)
    mq.reset_mq_backend()
    db.init_quant_db()
    yield db_url
    mq.reset_mq_backend()


class FakeMarket:
    """Mock 数据服务: 只实现风控/技术面用到的接口.

    ``quotes`` 由用例按需填充; 未配置的标的一律返回 None (模拟取数失败),
    让"行情缺失"成为显式场景而不是意外。
    """

    def __init__(self):
        self.quotes: dict[str, dict] = {}
        self.bars = 120
        self.indicators: dict[str, dict] = {}
        self.fail_quotes = False

    def get_realtime_quote(self, symbol):
        if self.fail_quotes:
            raise RuntimeError("quote source down")
        return self.quotes.get(symbol)

    def get_batch_quotes(self, symbols):
        if self.fail_quotes:
            raise RuntimeError("quote source down")
        return {s: self.quotes[s] for s in symbols if s in self.quotes}

    def get_ohlcv(self, symbol, lookback_days=120):
        import pandas as pd

        n = self.bars
        return pd.DataFrame({
            "Date": pd.date_range("2026-01-01", periods=n).astype(str),
            "Open": [10.0] * n,
            "High": [10.2] * n,
            "Low": [9.8] * n,
            "Close": [10.0] * n,
            "Volume": [1e6] * n,
        })

    def get_stock_indicators(self, symbol):
        return self.indicators.get(symbol)


class FakeBroker:
    """可控 broker: 现金 / 持仓 / 成交状态 / 取数异常 四个开关.

    只实现风控与卖出链路用到的语义 —— 成交后更新现金与持仓, 但不做
    手续费与 T+1 模拟 (那是 ``SimulatedBroker`` 的职责, 已在别处覆盖)。
    """

    broker_name = "fake"

    def __init__(self):
        self.cash: float = 1_000_000.0
        self.positions: list = []          # list[Position]
        self.prices: dict[str, float] = {}  # symbol -> 成交价
        self.order_status = None            # None 表示按 FILLED 处理
        self.fail_account = False
        self.fail_positions = False
        self.orders: list = []              # list[OrderRequest]

    # -- 构造辅助 ---------------------------------------------------------
    def add_position(self, symbol, quantity, cost, last_price, buy_date=""):
        from ai_stock.quant.broker import Position

        pos = Position(
            symbol=symbol,
            quantity=int(quantity),
            total_quantity=int(quantity),
            avg_cost=float(cost),
            last_price=float(last_price),
            market_value=int(quantity) * float(last_price),
            unrealized_pnl=int(quantity) * (float(last_price) - float(cost)),
            buy_date=buy_date,
        )
        self.positions.append(pos)
        return pos

    # -- BrokerAdapter ----------------------------------------------------
    def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        return None

    def get_account_info(self):
        from ai_stock.quant.broker import AccountInfo

        if self.fail_account:
            raise RuntimeError("account source down")
        market_value = sum(p.market_value for p in self.positions)
        return AccountInfo(
            total_assets=self.cash + market_value,
            available_cash=self.cash,
            frozen_cash=0.0,
            total_market_value=market_value,
        )

    def get_positions(self):
        if self.fail_positions:
            raise RuntimeError("position source down")
        return list(self.positions)

    def place_order(self, request):
        from ai_stock.quant.broker import OrderResult, OrderSide, OrderStatus

        self.orders.append(request)
        status = self.order_status or OrderStatus.FILLED
        price = float(self.prices.get(request.symbol, 0.0))
        if status != OrderStatus.FILLED or price <= 0:
            return OrderResult(
                order_id=f"fake-{len(self.orders)}", status=status, message="fake",
            )
        amount = price * request.quantity
        if request.side == OrderSide.BUY:
            self.cash -= amount
            self.add_position(request.symbol, request.quantity, price, price)
        else:
            self.cash += amount
            for pos in self.positions:
                if pos.symbol == request.symbol:
                    pos.quantity = max(pos.quantity - request.quantity, 0)
                    pos.total_quantity = max(pos.total_quantity - request.quantity, 0)
                    pos.market_value = pos.total_quantity * pos.last_price
                    break
        return OrderResult(
            order_id=f"fake-{len(self.orders)}",
            status=OrderStatus.FILLED,
            filled_quantity=request.quantity,
            avg_fill_price=price,
            amount=amount,
        )

    def cancel_order(self, order_id: str) -> bool:
        return True

    def query_order(self, order_id: str):
        from ai_stock.quant.broker import OrderResult, OrderStatus

        return OrderResult(order_id=order_id, status=OrderStatus.UNKNOWN)


@pytest.fixture()
def fake_broker(quant_env):
    """装配可控 broker; 用例结束后复位, 防跨用例串味."""
    from ai_stock.quant import broker as broker_mod

    fake = FakeBroker()
    broker_mod.set_broker(fake)
    yield fake
    broker_mod._broker = None


@pytest.fixture()
def fake_market(monkeypatch):
    """替换风控/持仓链路的数据服务, 并默认大盘走强.

    不在 ``risk_control`` 上替换 ``_market_timing_check`` —— 择时门用例需要
    真实实现, 替换后 monkeypatch 恢复只会恢复成桩。
    """
    from ai_stock.quant import market_regime, risk_control

    fake = FakeMarket()
    monkeypatch.setattr(risk_control, "get_data_service", lambda: fake)
    monkeypatch.setattr(
        market_regime, "get_market_regime",
        lambda force=False: {
            "regime": "risk_on", "ok": True, "reason": "沪深300 站稳 MA20",
        },
    )
    return fake

