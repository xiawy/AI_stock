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
