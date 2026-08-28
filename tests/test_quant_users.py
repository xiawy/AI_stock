"""多用户模拟账户测试: 资金配置 / 开户 / 买卖执行 / T+1 / 扇出 / 盈亏统计 / 迁移."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ai_stock.quant import db, db_ops, user_accounts as ua


@pytest.fixture()
def quant_db(tmp_path, monkeypatch):
    """临时 quant DB + 隔离资金配置 (不读真实 QUANT_CAPITAL_FILE/.env)."""
    db_url = f"sqlite:///{tmp_path / 'quant_users.db'}"
    monkeypatch.setenv("QUANT_DB_URL", db_url)
    monkeypatch.delenv("QUANT_INITIAL_CAPITAL", raising=False)
    monkeypatch.setenv("QUANT_CAPITAL_FILE", str(tmp_path / "no_such_capital.json"))
    db.reset_engine(db_url)
    db.init_quant_db()
    yield tmp_path


def _write_capital(tmp_path, data: dict) -> None:
    import json

    path = tmp_path / "capital.json"
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# 起始资金配置
# ---------------------------------------------------------------------------

def test_resolve_capital_env_default(quant_db, monkeypatch):
    assert ua.resolve_initial_capital("1") == ua.DEFAULT_INITIAL_CAPITAL
    monkeypatch.setenv("QUANT_INITIAL_CAPITAL", "500000")
    assert ua.resolve_initial_capital("1") == 500000.0


def test_resolve_capital_json_override(quant_db, monkeypatch):
    _write_capital(quant_db, {"default": 300000, "2": 800000, "alice": 400000})
    monkeypatch.setenv("QUANT_CAPITAL_FILE", str(quant_db / "capital.json"))
    # user_id 命中 > username 命中 > default
    assert ua.resolve_initial_capital("2", "alice") == 800000.0
    assert ua.resolve_initial_capital("9", "alice") == 400000.0
    assert ua.resolve_initial_capital("9", "bob") == 300000.0


def test_account_open_idempotent(quant_db):
    a1 = ua.get_or_create_account("1", "alice")
    assert a1["cash_balance"] == ua.DEFAULT_INITIAL_CAPITAL
    ua.execute_user_buy("1", "600000", price=10.0)
    a2 = ua.get_or_create_account("1", "alice")
    # 重复开户绝不重置资金
    assert a2["cash_balance"] < ua.DEFAULT_INITIAL_CAPITAL


# ---------------------------------------------------------------------------
# 买入执行
# ---------------------------------------------------------------------------

def test_user_buy_default_budget_and_fee(quant_db):
    result = ua.execute_user_buy("1", "600000", price=10.0, name="浦发银行")
    assert result["ok"] is True
    # 默认预算 = 1_000_000 × 20% = 200_000 → 20000 股
    assert result["quantity"] == 20000
    assert result["amount"] == pytest.approx(200000.0)
    # 佣金 50 + 过户费 2 = 52
    assert result["fee"] == pytest.approx(52.0)

    account = db_ops.get_user_account("1")
    assert account["cash_balance"] == pytest.approx(1_000_000 - 200000 - 52)

    holding = db_ops.get_holding("600000", user_id="1")
    assert holding["quantity"] == 20000
    assert holding["available_quantity"] == 0  # T+1 冻结
    assert holding["cost_price"] > 10.0  # 含手续费摊薄
    # 系统账户持仓池不受影响
    assert db_ops.get_holding("600000") is None


def test_user_buy_max_holding_count(quant_db):
    for i, sym in enumerate(("600000", "000001", "300750")):
        assert ua.execute_user_buy("1", sym, price=10.0)["ok"] is True
    r = ua.execute_user_buy("1", "600519", price=10.0)
    assert r["ok"] is False
    assert "上限" in r["reason"]


def test_user_buy_insufficient_cash(quant_db):
    # 指定数量超出 20% 预算的现金约束: 直接指定 12 万股 (120 万 > 100 万现金)
    r = ua.execute_user_buy("1", "600000", price=10.0, quantity=120000)
    assert r["ok"] is False
    assert "资金不足" in r["reason"]


# ---------------------------------------------------------------------------
# 卖出执行 + 单笔盈亏
# ---------------------------------------------------------------------------

def test_user_sell_t1_guard_then_full_close(quant_db, monkeypatch):
    buy = ua.execute_user_buy("1", "600000", price=10.0, quantity=1000)
    assert buy["ok"] is True

    # T+1: 当日禁卖
    r = ua.execute_user_sell("1", "600000", price=12.0)
    assert r["ok"] is False
    assert "T+1" in r["reason"]

    # 模拟进入下一交易日: T+1 窗口已过
    monkeypatch.setattr(ua, "_t1_active", lambda holding: False)

    sell = ua.execute_user_sell("1", "600000", price=12.0, reason="止盈")
    assert sell["ok"] is True
    assert sell["quantity"] == 1000
    assert sell["cleared"] is True
    cost = sell["cost_price"]
    assert cost > 10.0
    expected_fee = max(12000 * 0.00025, 5.0) + 12000 * 0.0005 + 12000 * 0.00001
    assert sell["fee"] == pytest.approx(expected_fee, abs=0.02)
    assert sell["realized_pnl"] == pytest.approx((12.0 - cost) * 1000 - sell["fee"], abs=0.02)
    assert sell["pnl_pct"] == pytest.approx((12.0 - cost) / cost, abs=1e-4)

    # 清仓后持仓消失, 现金回款
    assert db_ops.get_holding("600000", user_id="1") is None
    account = db_ops.get_user_account("1")
    assert account["cash_balance"] > 1_000_000  # 盈利卖出

    # 流水含盈亏字段
    trades = db_ops.get_trades(user_id="1")
    sell_log = next(t for t in trades if t["side"] == "sell")
    assert sell_log["realized_pnl"] == pytest.approx(sell["realized_pnl"])
    assert sell_log["pnl_pct"] == pytest.approx(sell["pnl_pct"])


def test_user_sell_partial_portion(quant_db, monkeypatch):
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    monkeypatch.setattr(ua, "_t1_lock_until", lambda: past)
    ua.execute_user_buy("1", "600000", price=10.0, quantity=1000)
    r = ua.execute_user_sell("1", "600000", portion=0.5, price=10.5)
    assert r["ok"] is True
    assert r["quantity"] == 500
    holding = db_ops.get_holding("600000", user_id="1")
    assert holding["quantity"] == 500
    assert holding["available_quantity"] == 500


def test_user_trade_stats(quant_db, monkeypatch):
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    monkeypatch.setattr(ua, "_t1_lock_until", lambda: past)
    # 一笔盈利 + 一笔亏损
    ua.execute_user_buy("1", "600000", price=10.0, quantity=1000)
    ua.execute_user_sell("1", "600000", price=12.0)
    ua.execute_user_buy("1", "000001", price=20.0, quantity=500)
    ua.execute_user_sell("1", "000001", price=18.0)

    stats = db_ops.get_user_trade_stats("1")
    assert stats["closed_trades"] == 2
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["win_rate"] == pytest.approx(0.5)
    assert stats["total_realized_pnl"] != 0


# ---------------------------------------------------------------------------
# 引擎扇出
# ---------------------------------------------------------------------------

def test_fanout_buy_and_sell_across_users(quant_db, monkeypatch):
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    monkeypatch.setattr(ua, "_t1_lock_until", lambda: past)
    ua.get_or_create_account("1", "alice")
    ua.get_or_create_account("2", "bob")
    db_ops.upsert_user_account({"user_id": "3", "username": "frozen",
                                "initial_capital": 100000, "status": "frozen"})

    summary = ua.fanout_buy("600000", price=10.0, name="浦发银行", reason="引擎建仓")
    assert summary["accounts"] == 2          # frozen 账户不参与
    assert summary["filled"] == 2
    assert db_ops.get_holding("600000", user_id="1") is not None
    assert db_ops.get_holding("600000", user_id="2") is not None

    sell = ua.fanout_sell("600000", portion=1.0, price=11.0, reason="止盈")
    assert sell["holders"] == 2
    assert sell["filled"] == 2
    assert db_ops.get_holdings("__all__") == []
    # 每个用户都记录了单笔盈亏
    for uid in ("1", "2"):
        stats = db_ops.get_user_trade_stats(uid)
        assert stats["closed_trades"] == 1
        assert stats["total_realized_pnl"] > 0


def test_fanout_sell_t1_skips_recent_buyers(quant_db):
    ua.get_or_create_account("1")
    ua.fanout_buy("600000", price=10.0)
    # 当日买入: T+1 禁卖 → 扇出跳过
    sell = ua.fanout_sell("600000", portion=1.0, price=11.0)
    assert sell["holders"] == 1
    assert sell["filled"] == 0
    assert db_ops.get_holding("600000", user_id="1") is not None


# ---------------------------------------------------------------------------
# 存量库迁移
# ---------------------------------------------------------------------------

def test_migrate_legacy_schema(tmp_path):
    from sqlalchemy import create_engine, text

    db_url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE quant_trade_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, order_id VARCHAR(64),"
            " symbol VARCHAR(16) NOT NULL, name VARCHAR(64),"
            " side VARCHAR(8) NOT NULL, price FLOAT, quantity INTEGER,"
            " amount FLOAT, fee FLOAT, status VARCHAR(16), reason TEXT,"
            " broker VARCHAR(32), trade_time DATETIME)"
        ))
        conn.execute(text(
            "CREATE TABLE quant_stock_pool_holding ("
            "symbol VARCHAR(16) PRIMARY KEY, name VARCHAR(64), quantity INTEGER)"
        ))
        conn.execute(text(
            "INSERT INTO quant_trade_log (symbol, side, price, quantity, status)"
            " VALUES ('600000', 'buy', 10.0, 1000, 'filled')"
        ))
    engine.dispose()

    db.reset_engine(db_url)
    db.init_quant_db()

    # 历史流水默认归属系统账户, 新增列可读
    trades = db_ops.get_trades(limit=10)
    assert len(trades) == 1
    assert trades[0]["user_id"] == "system"
    assert trades[0]["realized_pnl"] == 0.0

    # 持仓表已重建为复合主键 (旧表仅备份, 模拟盘数据不迁移)
    assert db_ops.get_holdings("__all__") == []
    db_ops.upsert_holding({"symbol": "000001", "quantity": 100}, user_id="7")
    assert db_ops.get_holding("000001", user_id="7")["quantity"] == 100
