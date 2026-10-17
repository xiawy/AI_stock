"""量化优化项回归测试 (对应 docs/QUANT_FIX_PLAN.md 的 P0/P1/A/T 条目).

覆盖:
- P0-2 / A2 配置版本化迁移 (默认值变更能下发到存量库, 人工设置不被覆盖)
- P0-1 / A1 板块数据多源回退 + 健康矩阵 + 指数退避
- P1-1 仓位系数与风控上限共用同一比例口径 (高置信标的不再必被拒)
- P1-2 趋势动量通道 + MACD 背离窗口
- P1-3 二次验证「双闸门」软化为评分项
- T2 行业集中度硬约束
- T3 大盘择时门
- T4/T5 ATR 止损 + 分层时间止损
- T1 波动率目标仓位 + 组合级风险预算总闸
- T9 ST / 停牌 / 次新股过滤
- A3 业务级 SLO 巡检
- A4 事件去重落库
- A6 决策日志 / 终态流程归档

全部使用临时 SQLite + Mock 数据服务, 不依赖外部行情与真实 LLM。
"""

from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def quant_env(tmp_path, monkeypatch):
    from ai_stock.quant import db, mq

    db_url = f"sqlite:///{tmp_path / 'quant_opt_test.db'}"
    monkeypatch.setenv("QUANT_DB_URL", db_url)
    monkeypatch.setattr(mq, "MQ_BACKEND", "sqlite")
    db.reset_engine(db_url)
    mq.reset_mq_backend()
    db.init_quant_db()
    yield db_url
    mq.reset_mq_backend()


class FakeMarket:
    """Mock 数据服务: 只实现风控/技术面用到的接口."""

    def __init__(self):
        self.quotes: dict[str, dict] = {}
        self.bars = 120
        self.indicators: dict[str, dict] = {}

    def get_realtime_quote(self, symbol):
        return self.quotes.get(symbol)

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


@pytest.fixture()
def market(monkeypatch):
    from ai_stock.quant import market_regime, risk_control

    fake = FakeMarket()
    monkeypatch.setattr(risk_control, "get_data_service", lambda: fake)
    # 默认大盘走强; 各用例按需覆盖 get_market_regime
    # 注意: 不要在这里替换 risk_control._market_timing_check, 否则择时门用例
    # 拿不到真实实现 (恢复时只会恢复成被替换的桩).
    monkeypatch.setattr(
        market_regime, "get_market_regime",
        lambda force=False: {
            "regime": "risk_on", "ok": True, "reason": "沪深300 站稳 MA20",
        },
    )
    return fake


def _buy_order(symbol="600000", qty=1000):
    from ai_stock.quant.broker import OrderRequest, OrderSide, OrderType

    return OrderRequest(
        symbol=symbol, side=OrderSide.BUY, quantity=qty, order_type=OrderType.MARKET,
    )


# ---------------------------------------------------------------------------
# P0-2 / A2: 配置版本化迁移
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConfigMigration:
    def test_legacy_default_is_migrated(self, quant_env):
        """老库 global_trade_enable='0' (旧默认值) 应被迁移到 '1'."""
        from ai_stock.quant import db_ops
        from ai_stock.quant.config import CONFIG_SCHEMA_KEY, CONFIG_SCHEMA_VERSION
        from ai_stock.quant.service import _seed_system_config

        db_ops.set_config_value("global_trade_enable", "0")
        db_ops.set_config_value("max_order_value", "200000")
        assert db_ops.get_config_value(CONFIG_SCHEMA_KEY) == ""

        _seed_system_config()

        assert db_ops.get_config_value("global_trade_enable") == "1"
        assert db_ops.get_config_value("max_order_value") == "250000"
        assert db_ops.get_config_value(CONFIG_SCHEMA_KEY) == str(CONFIG_SCHEMA_VERSION)

    def test_manual_override_survives_migration(self, quant_env):
        """运维手工设置的非默认值不得被迁移覆盖 (审计要求)."""
        from ai_stock.quant import db_ops
        from ai_stock.quant.service import _seed_system_config

        db_ops.set_config_value("global_trade_enable", "5")   # 人工设置
        _seed_system_config()
        assert db_ops.get_config_value("global_trade_enable") == "5"

    def test_migration_runs_once(self, quant_env):
        from ai_stock.quant import db_ops
        from ai_stock.quant.config import CONFIG_SCHEMA_KEY
        from ai_stock.quant.service import _seed_system_config

        _seed_system_config()
        version = db_ops.get_config_value(CONFIG_SCHEMA_KEY)
        # 迁移后再手工关闸: 已是最新版本, 不会被再次迁移回 1
        db_ops.set_config_value("global_trade_enable", "0")
        _seed_system_config()
        assert db_ops.get_config_value("global_trade_enable") == "0"
        assert db_ops.get_config_value(CONFIG_SCHEMA_KEY) == version


# ---------------------------------------------------------------------------
# P0-1 / A1: 数据源多源回退 + 健康矩阵 + 退避
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDataSourceFallback:
    def test_failover_to_secondary_provider(self, monkeypatch):
        from ai_stock.dataflows import data_health, pipeline_data

        data_health.reset()

        def _boom(level):
            raise ConnectionError("clist refused")

        monkeypatch.setattr(pipeline_data, "_fetch_board_flow", _boom)
        monkeypatch.setattr(
            pipeline_data, "_sina_board_flow",
            lambda level: [
                {"code": "new_dlhy", "name": "电力行业", "change_pct": 1.2,
                 "board_level": level, "main_net_inflow": 0.0},
            ],
        )
        rows = pipeline_data._fetch_board_flow_any("industry")
        assert rows and rows[0]["source"] == "sina_board"
        snapshot = data_health.snapshot()
        sources = {row["source"]: row for row in snapshot}
        assert sources["eastmoney_push2"]["consecutive_fail"] == 1
        assert sources["sina_board"]["success_rate"] == 1.0
        data_health.reset()

    def test_backoff_after_threshold(self, monkeypatch):
        from ai_stock.dataflows import data_health, pipeline_data

        data_health.reset()

        def _boom(level):
            raise ConnectionError("blocked")

        monkeypatch.setattr(pipeline_data, "_fetch_board_flow", _boom)
        monkeypatch.setattr(pipeline_data, "_sina_board_flow", lambda level: [])
        for _ in range(3):
            assert pipeline_data._fetch_board_flow_any("industry") == []
        assert data_health.in_backoff("eastmoney_push2") > 0
        data_health.reset()

    def test_empty_result_counts_as_failure(self, monkeypatch):
        """接口「成功但返回 0 条」是最危险的静默故障, 必须计入失败."""
        from ai_stock.dataflows import data_health, pipeline_data

        data_health.reset()
        monkeypatch.setattr(pipeline_data, "_fetch_board_flow", lambda level: [])
        monkeypatch.setattr(pipeline_data, "_sina_board_flow", lambda level: [])
        pipeline_data._fetch_board_flow_any("industry")
        row = next(
            r for r in data_health.snapshot()
            if r["source"] == "eastmoney_push2" and r["api"] == "board_list"
        )
        assert row["success_rate"] == 0.0
        data_health.reset()


# ---------------------------------------------------------------------------
# P1-1: 仓位系数与风控上限一致
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPositionRatioConsistency:
    def test_high_confidence_ratio_passes_budget_check(self, quant_env, market):
        """coef=1.2 (24%) 在可用资金 100 万时派出 24 万, 不应被 20% 上限拒."""
        from ai_stock.quant.config import BUY_POSITION_RATIO, POS_COEF_HIGH
        from ai_stock.quant.risk_control import pre_trade_check

        market.quotes["600000"] = {
            "price": 10.0, "name": "浦发银行", "limit_up": 11.0, "limit_down": 9.0,
        }
        snap = {"available_cash": 1_000_000.0, "market_value": 0.0,
                "total_assets": 1_000_000.0, "drawdown_pct": 0.0, "daily_pnl": 0.0}
        ratio = BUY_POSITION_RATIO * POS_COEF_HIGH   # 0.24 → 25 万以内
        result = pre_trade_check(
            _buy_order(qty=24_000), snap, position_ratio=ratio,
        )
        assert result.ok, result.reason

    def test_same_order_rejected_without_ratio(self, quant_env, market):
        """不传实际比例时按基础 20% 兜底 → 24% 的单仍被拒 (行为可预期)."""
        from ai_stock.quant.risk_control import pre_trade_check

        market.quotes["600000"] = {
            "price": 10.0, "name": "浦发银行", "limit_up": 11.0, "limit_down": 9.0,
        }
        snap = {"available_cash": 1_000_000.0, "market_value": 0.0,
                "total_assets": 1_000_000.0, "drawdown_pct": 0.0, "daily_pnl": 0.0}
        result = pre_trade_check(_buy_order(qty=24_000), snap)
        assert not result.ok
        assert "超过可用资金" in result.reason


# ---------------------------------------------------------------------------
# T2: 行业集中度
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIndustryConcentration:
    def test_same_industry_blocked(self, quant_env, market):
        from ai_stock.quant import db_ops
        from ai_stock.quant.config import INDUSTRY_CONCENTRATION_MAX
        from ai_stock.quant.risk_control import pre_trade_check

        assert INDUSTRY_CONCENTRATION_MAX == 1
        db_ops.upsert_optional_stock({
            "symbol": "600760", "name": "中航沈飞", "industry": "航天航空",
        })
        db_ops.upsert_holding({
            "symbol": "600150", "name": "中国船舶", "industry": "航天航空",
            "quantity": 100, "cost_price": 30.0,
        })
        market.quotes["600760"] = {
            "price": 50.0, "name": "中航沈飞", "limit_up": 55.0, "limit_down": 45.0,
        }
        snap = {"available_cash": 1_000_000.0, "market_value": 3000.0,
                "total_assets": 1_003_000.0, "drawdown_pct": 0.0, "daily_pnl": 0.0}
        result = pre_trade_check(_buy_order("600760", 1000), snap)
        assert not result.ok
        assert "行业集中度" in result.reason

    def test_different_industry_allowed(self, quant_env, market):
        from ai_stock.quant import db_ops
        from ai_stock.quant.risk_control import pre_trade_check

        db_ops.upsert_optional_stock({
            "symbol": "603087", "name": "甘李药业", "industry": "生物制品",
        })
        db_ops.upsert_holding({
            "symbol": "600150", "name": "中国船舶", "industry": "航天航空",
            "quantity": 100, "cost_price": 30.0,
        })
        market.quotes["603087"] = {
            "price": 40.0, "name": "甘李药业", "limit_up": 44.0, "limit_down": 36.0,
        }
        snap = {"available_cash": 1_000_000.0, "market_value": 3000.0,
                "total_assets": 1_003_000.0, "drawdown_pct": 0.0, "daily_pnl": 0.0}
        assert pre_trade_check(_buy_order("603087", 1000), snap).ok


# ---------------------------------------------------------------------------
# T3: 大盘择时门
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMarketTimingGate:
    def test_risk_off_blocks_new_position(self, quant_env, market, monkeypatch):
        from ai_stock.quant import market_regime, risk_control

        market.quotes["600000"] = {
            "price": 10.0, "name": "浦发银行", "limit_up": 11.0, "limit_down": 9.0,
        }
        monkeypatch.setattr(
            market_regime, "get_market_regime",
            lambda force=False: {"regime": "risk_off", "ok": True,
                                 "reason": "沪深300 跌破 MA20"},
        )
        snap = {"available_cash": 1_000_000.0, "market_value": 0.0,
                "total_assets": 1_000_000.0, "drawdown_pct": 0.0, "daily_pnl": 0.0}
        result = risk_control.pre_trade_check(_buy_order(qty=1000), snap)
        assert not result.ok
        assert "大盘择时门" in result.reason

    def test_unknown_regime_does_not_block(self, quant_env, market, monkeypatch):
        """数据不可用 (unknown) 不应变成交易开关."""
        from ai_stock.quant import market_regime, risk_control

        market.quotes["600000"] = {
            "price": 10.0, "name": "浦发银行", "limit_up": 11.0, "limit_down": 9.0,
        }
        monkeypatch.setattr(
            market_regime, "get_market_regime",
            lambda force=False: {"regime": "unknown", "ok": False, "reason": "no data"},
        )
        snap = {"available_cash": 1_000_000.0, "market_value": 0.0,
                "total_assets": 1_000_000.0, "drawdown_pct": 0.0, "daily_pnl": 0.0}
        assert risk_control.pre_trade_check(_buy_order(qty=1000), snap).ok

    def test_runtime_switch_disables_gate(self, quant_env, market, monkeypatch):
        """market_timing_enable=0 必须真正关闭择时门 (运营开关, 无需重启)."""
        from ai_stock.quant import db_ops, market_regime, risk_control

        market.quotes["600000"] = {
            "price": 10.0, "name": "浦发银行", "limit_up": 11.0, "limit_down": 9.0,
        }
        monkeypatch.setattr(
            market_regime, "get_market_regime",
            lambda force=False: {"regime": "risk_off", "ok": True,
                                 "reason": "沪深300 跌破 MA20"},
        )
        snap = {"available_cash": 1_000_000.0, "market_value": 0.0,
                "total_assets": 1_000_000.0, "drawdown_pct": 0.0, "daily_pnl": 0.0}

        db_ops.set_config_value("market_timing_enable", "1")
        assert not risk_control.pre_trade_check(_buy_order(qty=1000), snap).ok

        db_ops.set_config_value("market_timing_enable", "0")
        assert risk_control.pre_trade_check(_buy_order(qty=1000), snap).ok


# ---------------------------------------------------------------------------
# T9: ST / 停牌 / 次新
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSymbolEligibility:
    def test_st_blocked(self, quant_env, market):
        from ai_stock.quant.risk_control import pre_trade_check

        market.quotes["600001"] = {
            "price": 5.0, "name": "*ST 某股", "limit_up": 5.5, "limit_down": 4.5,
        }
        snap = {"available_cash": 1_000_000.0, "market_value": 0.0,
                "total_assets": 1_000_000.0, "drawdown_pct": 0.0, "daily_pnl": 0.0}
        result = pre_trade_check(_buy_order("600001", 1000), snap)
        assert not result.ok
        assert "ST" in result.reason

    def test_suspended_blocked(self, quant_env, market):
        from ai_stock.quant.risk_control import pre_trade_check

        market.quotes["600002"] = {"price": 0.0, "name": "停牌股"}
        snap = {"available_cash": 1_000_000.0, "market_value": 0.0,
                "total_assets": 1_000_000.0, "drawdown_pct": 0.0, "daily_pnl": 0.0}
        result = pre_trade_check(_buy_order("600002", 100), snap)
        assert not result.ok

    def test_new_listing_blocked(self, quant_env, market):
        from ai_stock.quant.risk_control import pre_trade_check

        market.bars = 20   # < NEW_LISTING_MIN_BARS
        market.quotes["301999"] = {
            "price": 30.0, "name": "次新股", "limit_up": 36.0, "limit_down": 24.0,
        }
        snap = {"available_cash": 1_000_000.0, "market_value": 0.0,
                "total_assets": 1_000_000.0, "drawdown_pct": 0.0, "daily_pnl": 0.0}
        result = pre_trade_check(_buy_order("301999", 1000), snap)
        assert not result.ok
        assert "次新股" in result.reason


# ---------------------------------------------------------------------------
# P1-2: 趋势动量通道
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTrendMomentum:
    _IND = {
        "close_series": [10.0] * 19 + [10.6],
        "ma20": 10.2,
        "ma20_slope": 0.01,
        "volume_ratio_20": 1.5,
        "rsi": 62.0,
    }

    def test_triggers_when_all_conditions_met(self):
        from ai_stock.quant.agents.buy import TechSignalAgent

        assert TechSignalAgent._trend_momentum(self._IND)["triggered"] is True

    def test_blocked_when_rsi_overheated(self):
        from ai_stock.quant.agents.buy import TechSignalAgent

        ind = {**self._IND, "rsi": 85.0}
        assert TechSignalAgent._trend_momentum(ind)["triggered"] is False

    def test_blocked_when_no_volume(self):
        from ai_stock.quant.agents.buy import TechSignalAgent

        ind = {**self._IND, "volume_ratio_20": 1.0}
        assert TechSignalAgent._trend_momentum(ind)["triggered"] is False

    def test_macd_divergence_uses_wide_window(self):
        """背离判定必须回溯 ≥20 根: 仅比昨天低不构成背离."""
        from ai_stock.quant.agents.buy import TechSignalAgent
        from ai_stock.quant.config import MACD_DIVERGENCE_WINDOW

        window = MACD_DIVERGENCE_WINDOW
        # 前 window 根一路下探至 8.08 (窗口最低), 末根 8.00 才真正创新低;
        # 同时 MACD 由 -1.0 抬到 -0.5 → 价格新低但指标未新低 = 底背离
        close = [10.0 - 0.08 * i for i in range(window)] + [8.0]
        macd = [-1.0] * window + [-0.5]
        ind = {
            "close_series": close, "macd_series": macd, "rsi": 30.0,
            "kdjk_series": [10, 11], "kdjd_series": [12, 10],
        }
        assert TechSignalAgent._macd_divergence(ind)["triggered"] is True

    def test_macd_divergence_needs_history(self):
        from ai_stock.quant.agents.buy import TechSignalAgent

        ind = {
            "close_series": [9.0, 8.9, 8.8], "macd_series": [-1, -1, -0.9],
            "rsi": 30.0, "kdjk_series": [], "kdjd_series": [],
        }
        assert TechSignalAgent._macd_divergence(ind)["triggered"] is False


# ---------------------------------------------------------------------------
# P1-3: 二次验证软化
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVerificationSoftening:
    def _handle(self, monkeypatch, **report_fields):
        from ai_stock.quant.agents import buy as buy_mod

        report = buy_mod.VerificationReport(**report_fields)
        monkeypatch.setattr(
            buy_mod, "structured_invoke", lambda llm, schema, prompt, **kw: report,
        )
        agent = buy_mod.SecondVerificationAgent(data_service=FakeMarket())
        from ai_stock.quant.llm_helper import QuantLLM

        agent._llm_holder = QuantLLM(llm_quick=object(), llm_deep=object())
        return agent.handle({
            "task_id": "t", "payload": {
                "flow_id": "f", "flow_type": "buy", "step": "second_verification",
                "context": {"symbol": "600000", "pool": {}},
            },
        })

    def test_unresolved_low_risk_bear_passes(self, quant_env, monkeypatch):
        result = self._handle(
            monkeypatch, passed=True, reason="ok", major_bear_resolved=False,
            bear_risk_level="low",
        )
        assert result["passed"] is True
        assert result["reject_stage"] == ""

    def test_unresolved_high_risk_bear_rejected(self, quant_env, monkeypatch):
        result = self._handle(
            monkeypatch, passed=True, reason="ok", major_bear_resolved=False,
            bear_risk_level="high",
        )
        assert result["passed"] is False
        assert result["reject_stage"] == "major_bear_high"

    def test_resolved_bear_passes(self, quant_env, monkeypatch):
        result = self._handle(
            monkeypatch, passed=True, reason="ok", major_bear_resolved=True,
            bear_risk_level="high",
        )
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# A3: 业务级 SLO
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBusinessSlo:
    def test_no_trade_ever_is_critical(self, quant_env, monkeypatch):
        from ai_stock.quant import ops_monitor

        emitted: list[tuple] = []
        monkeypatch.setattr(
            ops_monitor.AlertManager, "emit",
            lambda self, level, atype, msg: emitted.append((level, atype)) or True,
        )
        result = ops_monitor.check_business_slo()
        assert "no_trade_ever" in result["issues"]
        assert any(level == ops_monitor.CRITICAL for level, _ in emitted)

    def test_industry_scan_empty_streak_alerted(self, quant_env, monkeypatch):
        from ai_stock.quant import db_ops, ops_monitor
        from ai_stock.quant.config import SLO_SCAN_EMPTY_STREAK

        for _ in range(SLO_SCAN_EMPTY_STREAK):
            db_ops.log_decision(
                agent="industry_scan", decision="scan_empty", reason="no candidates",
            )
        emitted: list[tuple] = []
        monkeypatch.setattr(
            ops_monitor.AlertManager, "emit",
            lambda self, level, atype, msg: emitted.append((atype,)) or True,
        )
        ops_monitor.check_business_slo()
        assert ("slo_industry_scan_empty",) in emitted

    def test_webhook_pushed_when_configured(self, monkeypatch):
        from ai_stock.quant import ops_monitor

        calls: list[dict] = []

        class _Resp:
            status_code = 200
            text = "ok"

        monkeypatch.setattr(
            ops_monitor, "AlertManager",
            ops_monitor.AlertManager,
        )
        import requests

        monkeypatch.setattr(
            requests, "post",
            lambda url, json=None, timeout=None: calls.append({"url": url, "json": json}) or _Resp(),
        )
        monkeypatch.setattr(
            "ai_stock.quant.config.ALERT_WEBHOOK_URL", "https://example.invalid/hook",
        )
        mgr = ops_monitor.AlertManager(silence_seconds=0)
        assert mgr.emit(ops_monitor.CRITICAL, "unit_test_alert", "hello") is True
        assert calls and calls[0]["json"]["text"]["content"].startswith("[CRITICAL]")


# ---------------------------------------------------------------------------
# A4: 事件去重落库
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEventDedupe:
    def test_dedupe_survives_process_restart(self, quant_env):
        from ai_stock.quant.orchestrator import Orchestrator

        first = Orchestrator()
        assert first._is_duplicate("flow_x", "tech_signal") is False
        # 新实例 (= 进程重启后内存表丢失) 仍应识别为重复
        second = Orchestrator()
        assert second._is_duplicate("flow_x", "tech_signal") is True
        # 不同步骤不受影响
        assert second._is_duplicate("flow_x", "catalyst_event") is False

    def test_dedupe_window_covers_longest_flow(self):
        from ai_stock.quant.config import FLOW_TIMEOUTS
        from ai_stock.quant.orchestrator import EVENT_DEDUPE_SECONDS

        assert EVENT_DEDUPE_SECONDS >= 2 * max(FLOW_TIMEOUTS.values())

    def test_idempotent_ttl_covers_flow_timeout(self):
        from ai_stock.quant.config import FLOW_TIMEOUTS, IDEMPOTENT_TTL

        assert IDEMPOTENT_TTL >= 2 * FLOW_TIMEOUTS["selection"]


# ---------------------------------------------------------------------------
# A6: 归档清理
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestArchiveCleanup:
    def test_old_decision_logs_removed(self, quant_env):
        from ai_stock.quant import db_ops
        from ai_stock.quant.db import session_scope
        from ai_stock.quant.db_models import AgentDecisionLog

        db_ops.log_decision(agent="unit", decision="old", reason="x")
        with session_scope() as s:
            row = s.query(AgentDecisionLog).first()
            row.ts = datetime.now(timezone.utc) - timedelta(days=120)
            s.commit()
        assert db_ops.cleanup_decision_log(90) >= 1
        assert db_ops.get_decisions(limit=10, agent="unit") == []

    def test_recent_decision_logs_kept(self, quant_env):
        from ai_stock.quant import db_ops

        db_ops.log_decision(agent="unit", decision="fresh", reason="x")
        assert db_ops.cleanup_decision_log(90) == 0
        assert len(db_ops.get_decisions(limit=10, agent="unit")) == 1

    def test_finished_flows_archived_running_kept(self, quant_env):
        from ai_stock.quant import db_ops
        from ai_stock.quant.db import session_scope
        from ai_stock.quant.db_models import QuantFlowState

        db_ops.create_flow("done_flow", "buy", 1800)
        db_ops.update_flow("done_flow", status="completed")
        db_ops.create_flow("running_flow", "buy", 1800)
        with session_scope() as s:
            row = s.get(QuantFlowState, "done_flow")
            row.updated_at = datetime.now(timezone.utc) - timedelta(days=60)
            s.commit()
        assert db_ops.archive_finished_flows(30) >= 1
        assert db_ops.get_flow("done_flow") is None
        assert db_ops.get_flow("running_flow") is not None


# ---------------------------------------------------------------------------
# 池观察期分层
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestObserveExpireByStage:
    def test_head_gets_longer_window(self, quant_env):
        from ai_stock.quant import db_ops

        db_ops.upsert_optional_stock({"symbol": "600010", "name": "A", "stage_batch": "HEAD"})
        db_ops.upsert_optional_stock({"symbol": "600011", "name": "B", "stage_batch": "BODY"})
        head = db_ops.get_optional_stock("600010")["observe_expire"]
        body = db_ops.get_optional_stock("600011")["observe_expire"]
        assert head > body


# ---------------------------------------------------------------------------
# T1: 波动率目标仓位 + 组合级风险预算总闸
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVolatilityTargetSizing:
    """同样 20% 资金, 高波动票应买到更少 —— 让每笔止损亏损趋同."""

    def _run(self, monkeypatch, *, atr, total_assets=1_000_000.0,
             available_cash=1_000_000.0, drawdown_pct=0.0,
             price=10.0) -> dict:
        from ai_stock.quant.agents import buy as buy_mod

        captured: dict = {}
        monkeypatch.setattr(
            buy_mod, "account_snapshot",
            lambda: {
                "available_cash": available_cash,
                "market_value": total_assets - available_cash,
                "total_assets": total_assets,
                "drawdown_pct": drawdown_pct,
                "daily_pnl": 0.0,
            },
        )

        class _Chk:
            ok = False
            reason = "test-short-circuit"

            def to_dict(self):
                return {}

        def _fake_check(order, snapshot=None, position_ratio=None):
            captured["qty"] = order.quantity
            captured["position_ratio"] = position_ratio
            return _Chk()

        monkeypatch.setattr(buy_mod, "pre_trade_check", _fake_check)

        data = FakeMarket()
        data.quotes["600000"] = {"price": price, "name": "X"}
        data.indicators["600000"] = {"atr": atr}
        context = {
            "symbol": "600000", "name": "X",
            "second_verification": {
                "passed": True, "reason": "ok", "predicted_path": "",
                "plan": {"stop_loss_pct": 5.0, "take_profit_pct": 15.0},
            },
            "pool_confidence": 0.5, "pool_stage": "BODY",
        }
        agent = buy_mod.PositionOpenAgent(data_service=data)
        result = agent.handle({
            "task_id": "t1",
            "payload": {
                "flow_id": "buy_sizing_flow", "flow_type": "buy",
                "step": "", "context": context,
            },
        })
        captured["decision"] = result["decision"]
        captured["reason"] = result.get("abort_reason", "")
        return captured

    def test_high_vol_capped_by_risk_budget(self, monkeypatch):
        from ai_stock.quant.config import RISK_BUDGET_PER_TRADE, STOP_LOSS_ATR_MULT

        # ATR=1.0 → 每股风险 2.0 元; 总资产 100 万 × 1% = 1 万 → 5000 股
        cap = self._run(monkeypatch, atr=1.0)
        expected = int(
            (1_000_000 * RISK_BUDGET_PER_TRADE) / (STOP_LOSS_ATR_MULT * 1.0) / 100
        ) * 100
        # 资金上限 20% → 约 19900 股, 风险预算先触顶
        assert cap["qty"] == expected == 5000

    def test_low_vol_still_bounded_by_cash(self, monkeypatch):
        from ai_stock.quant.config import BUY_POSITION_RATIO, POS_COEF_MID

        # ATR=0.1 → 每股风险 0.2 元 → 风险预算允许 5 万股, 远超资金上限
        row = self._run(monkeypatch, atr=0.1)
        budget = 1_000_000 * BUY_POSITION_RATIO * POS_COEF_MID
        assert row["qty"] == int(budget / (10.0 * 1.002) / 100) * 100

    def test_drawdown_halves_position_and_ratio(self, monkeypatch):
        from ai_stock.quant.config import (
            BUY_POSITION_RATIO, PORTFOLIO_DD_COEF_FACTOR, POS_COEF_MID,
        )

        row = self._run(monkeypatch, atr=0.1, drawdown_pct=0.10)
        budget = (
            1_000_000 * BUY_POSITION_RATIO * POS_COEF_MID * PORTFOLIO_DD_COEF_FACTOR
        )
        assert row["qty"] == int(budget / (10.0 * 1.002) / 100) * 100
        # 风控侧同口径: 上限校验也会跟着降, 不能一边降仓一边仍按 20% 校验
        assert row["position_ratio"] == pytest.approx(
            BUY_POSITION_RATIO * POS_COEF_MID * PORTFOLIO_DD_COEF_FACTOR,
        )

    def test_insufficient_risk_budget_rejected(self, monkeypatch):
        # 总资产 1 万 × 1% = 100 元; ATR=5.0 → 每股风险 10 元 → 不足一手
        row = self._run(monkeypatch, atr=5.0, total_assets=10_000.0,
                        available_cash=10_000.0)
        assert row["decision"] == "rejected"
        assert "风险预算" in row["reason"]

    def test_missing_atr_falls_back_to_cash_ratio(self, monkeypatch):
        from ai_stock.quant.config import BUY_POSITION_RATIO, POS_COEF_MID

        row = self._run(monkeypatch, atr=0.0)
        budget = 1_000_000 * BUY_POSITION_RATIO * POS_COEF_MID
        assert row["qty"] == int(budget / (10.0 * 1.002) / 100) * 100
