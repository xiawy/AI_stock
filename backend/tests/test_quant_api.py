"""Tests for the quant admin API (merged into the backend app).

Quant engine DB is redirected to a temp SQLite file; the MQ stays on the
sqlite fallback backend. The quant *service* (consumers/scheduler) is not
started here (``AISTOCK_DISABLE_SCHEDULERS=1`` in conftest) — read/trigger
endpoints must still work, triggers only enqueue.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def quant_ready(client, tmp_path, monkeypatch):
    """Temp quant DB + sqlite MQ + fresh API bootstrap flag."""
    from ai_stock.quant import db, mq
    from app.api import quant as quant_api

    db_url = f"sqlite:///{tmp_path / 'quant_api_test.db'}"
    monkeypatch.setenv("QUANT_DB_URL", db_url)
    monkeypatch.setattr(mq, "MQ_BACKEND", "sqlite")
    db.reset_engine(db_url)
    mq.reset_mq_backend()
    monkeypatch.setattr(quant_api, "_READY", False)
    yield
    monkeypatch.setattr(quant_api, "_READY", False)
    mq.reset_mq_backend()


def test_quant_requires_auth(client, quant_ready):
    resp = client.get("/api/quant/status")
    assert resp.status_code == 401


def test_status_seeded_with_trade_switch_off(client, auth_headers, quant_ready):
    resp = client.get("/api/quant/status", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # §15-1: 初始化后全局交易开关默认关闭
    assert data["config"]["global_trade_enable"] == "0"
    assert data["pending_tasks"] == 0
    assert "optional_pool_active" in data


def test_config_update_roundtrip(client, auth_headers, quant_ready):
    resp = client.put(
        "/api/quant/config",
        json={"key": "global_trade_enable", "value": "1"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["value"] == "1"
    cfg = client.get("/api/quant/config", headers=auth_headers).json()["config"]
    assert cfg["global_trade_enable"] == "1"


def test_pool_manual_status_flow(client, auth_headers, quant_ready):
    from ai_stock.quant import db, db_ops

    db.init_quant_db()  # 测试直连 db_ops, 需先建表 (API 路径由 _ensure_ready 负责)
    db_ops.upsert_optional_stock({"symbol": "600000", "name": "浦发银行"})

    pool = client.get("/api/quant/pool", headers=auth_headers).json()["pool"]
    assert any(item["symbol"] == "600000" for item in pool)

    resp = client.put(
        "/api/quant/pool/600000/status",
        json={"status": "hold"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    item = client.get("/api/quant/pool/600000", headers=auth_headers).json()
    assert item["status"] == "hold"

    # 未知标的一律 404
    resp = client.put(
        "/api/quant/pool/999999/status",
        json={"status": "removed"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_rules_seeded_and_approval_guard(client, auth_headers, quant_ready):
    rules = client.get("/api/quant/rules", headers=auth_headers).json()["rules"]
    rule_ids = {r["rule_id"] for r in rules}
    assert {"stop_loss_5pct", "time_stop_5days", "trailing_stop"} <= rule_ids

    # 种子规则自带单测且全部通过
    resp = client.post(
        "/api/quant/rules/stop_loss_5pct/test", headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["passed"] is True

    # 没有 pending_review 进化记录时, 审核接口必须拒绝 (§10.2 强制流水线)
    resp = client.post(
        "/api/quant/rules/stop_loss_5pct/approve",
        json={"note": ""},
        headers=auth_headers,
    )
    assert resp.status_code == 409


def test_rule_toggle(client, auth_headers, quant_ready):
    resp = client.put(
        "/api/quant/rules/stop_loss_5pct",
        json={"enabled": False},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    rules = client.get("/api/quant/rules", headers=auth_headers).json()["rules"]
    rule = next(r for r in rules if r["rule_id"] == "stop_loss_5pct")
    assert rule["enabled"] is False

    resp = client.put(
        "/api/quant/rules/not_exist", json={"enabled": True}, headers=auth_headers
    )
    assert resp.status_code == 404


def test_trigger_selection_enqueues(client, auth_headers, quant_ready, monkeypatch):
    from ai_stock.quant import scheduler

    # 手动触发与日期无关: 强制视为交易时段
    monkeypatch.setattr(scheduler, "is_trading_day", lambda dt=None: True)
    monkeypatch.setattr(scheduler, "is_trading_time", lambda dt=None: True)

    resp = client.post("/api/quant/trigger/selection", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["enqueued"] is True
    assert data["task_id"]

    # 未知动作拒绝
    resp = client.post("/api/quant/trigger/whatever", headers=auth_headers)
    assert resp.status_code == 422


def test_daily_report_generated(client, auth_headers, quant_ready):
    resp = client.get("/api/quant/daily-report", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    report = resp.json()["report"]
    assert report["global_trade_enable"] == "0"
    assert report["trades"]["count"] == 0


# ---------------------------------------------------------------------------
# 用户模拟账户 (/quant/me) 与多用户管理视图
# ---------------------------------------------------------------------------


def test_me_account_auto_open(client, auth_headers, quant_ready):
    resp = client.get("/api/quant/me/account", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["account"]["username"] == "tester"
    # 首次访问自动开户, 按默认起始资金 (1_000_000)
    assert data["cash_balance"] == 1000000.0
    assert data["total_assets"] == 1000000.0
    assert data["holdings"] == []


def test_me_manual_trade_flow_and_stats(client, auth_headers, quant_ready, monkeypatch):
    buy = client.post(
        "/api/quant/me/buy",
        json={"symbol": "600000", "quantity": 1000, "price": 10.0},
        headers=auth_headers,
    )
    assert buy.status_code == 200, buy.text
    assert buy.json()["quantity"] == 1000

    holdings = client.get("/api/quant/me/holdings", headers=auth_headers).json()["holdings"]
    assert len(holdings) == 1 and holdings[0]["symbol"] == "600000"

    # T+1: 当日买入不可卖 (400 + 原因)
    sell = client.post(
        "/api/quant/me/sell",
        json={"symbol": "600000", "price": 12.0},
        headers=auth_headers,
    )
    assert sell.status_code == 400
    assert "T+1" in sell.json()["detail"]

    # 模拟进入下一交易日后卖出盈利, 自动记录单笔盈亏
    from ai_stock.quant import user_accounts

    monkeypatch.setattr(user_accounts, "_t1_active", lambda holding: False)
    sell = client.post(
        "/api/quant/me/sell",
        json={"symbol": "600000", "price": 12.0},
        headers=auth_headers,
    )
    assert sell.status_code == 200, sell.text
    body = sell.json()
    assert body["cleared"] is True
    assert body["realized_pnl"] > 0
    assert body["pnl_pct"] > 0

    trades = client.get("/api/quant/me/trades", headers=auth_headers).json()["trades"]
    assert {t["side"] for t in trades} == {"buy", "sell"}

    stats = client.get("/api/quant/me/stats", headers=auth_headers).json()
    assert stats["closed_trades"] == 1
    assert stats["wins"] == 1
    assert stats["total_realized_pnl"] > 0


def test_admin_holdings_and_accounts_multi_user(client, auth_headers, quant_ready):
    from ai_stock.quant import db, db_ops

    db.init_quant_db()
    db_ops.upsert_user_account(
        {"user_id": "1", "username": "tester", "initial_capital": 1000000},
    )
    db_ops.upsert_user_account(
        {"user_id": "2", "username": "other", "initial_capital": 500000},
    )
    db_ops.upsert_holding({"symbol": "600000", "quantity": 100}, user_id="1")
    db_ops.upsert_holding({"symbol": "000001", "quantity": 200}, user_id="2")
    db_ops.add_trade_log({
        "user_id": "1", "symbol": "600000", "side": "buy",
        "price": 10.0, "quantity": 100, "amount": 1000.0,
    })

    # 管理视图默认全部用户, 可按 user_id 过滤
    all_holdings = client.get("/api/quant/holdings", headers=auth_headers).json()["holdings"]
    assert {h["symbol"] for h in all_holdings} == {"600000", "000001"}
    mine = client.get(
        "/api/quant/holdings?user_id=1", headers=auth_headers,
    ).json()["holdings"]
    assert [h["symbol"] for h in mine] == ["600000"]

    trades = client.get("/api/quant/trades", headers=auth_headers).json()["trades"]
    assert len(trades) == 1 and trades[0]["user_id"] == "1"

    accounts = client.get("/api/quant/accounts", headers=auth_headers).json()["accounts"]
    assert {a["user_id"] for a in accounts} == {"1", "2"}
    user1 = next(a for a in accounts if a["user_id"] == "1")
    assert user1["holdings"] == 1

    # status 总览含全量持仓与账户数
    status_info = client.get("/api/quant/status", headers=auth_headers).json()
    assert status_info["holdings"] == 2
    assert status_info["user_accounts"] == 2
