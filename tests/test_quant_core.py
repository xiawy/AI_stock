"""Quant 子系统核心测试: QuantService 装配 / 调度层 / 规则引擎.

覆盖设计文档: §5 (调度只入队 + 幂等), §10.1/10.2 (规则引擎与上线流水线),
§13 (system_config 种子), §15-1 (初始后交易开关默认关闭).
全部走 SQLite 降级队列 + 临时 DB, 不依赖 Redis/外部行情.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture()
def quant_env(tmp_path, monkeypatch):
    """独立临时 quant DB + SQLite 队列后端."""
    from ai_stock.quant import db, mq

    db_url = f"sqlite:///{tmp_path / 'quant_test.db'}"
    monkeypatch.setenv("QUANT_DB_URL", db_url)
    monkeypatch.setattr(mq, "MQ_BACKEND", "sqlite")
    db.reset_engine(db_url)
    mq.reset_mq_backend()
    db.init_quant_db()
    yield db_url
    mq.reset_mq_backend()


@pytest.mark.unit
class TestQuantService:
    def test_start_seeds_config_and_rules_then_stop(self, quant_env):
        from ai_stock.quant import db_ops
        from ai_stock.quant.service import QuantService

        service = QuantService(with_scheduler=False)
        service.start()
        try:
            # §15-1: 初始化后全局交易开关默认关闭
            assert db_ops.get_config_value("global_trade_enable") == "0"
            assert db_ops.get_config_value("max_daily_orders") == "50"
            # §10.1: 种子规则已写入
            rule_ids = {r["rule_id"] for r in db_ops.get_rules()}
            assert {"stop_loss_5pct", "time_stop_5days", "trailing_stop"} <= rule_ids
            # 消费者全部启动 (5 队列)
            assert len(service.consumers) == 5
            assert service.started

            status = service.status()
            assert status["mq_backend"] == "sqlite"
            assert status["global_trade_enable"] == "0"
        finally:
            service.stop()
        assert not service.started

    def test_start_is_idempotent_and_config_not_overwritten(self, quant_env):
        from ai_stock.quant import db_ops
        from ai_stock.quant.service import QuantService

        service = QuantService(with_scheduler=False)
        service.start()
        service.start()  # 重复启动无效
        try:
            # 运维手工修改的配置不会被种子覆盖
            db_ops.set_config_value("global_trade_enable", "1")
            from ai_stock.quant.service import _seed_system_config

            _seed_system_config()
            assert db_ops.get_config_value("global_trade_enable") == "1"
        finally:
            service.stop()

    def test_maintenance_task_consumed_end_to_end(self, quant_env):
        """pool_expire 任务入队后被 risk 队列消费者处理 (闭环)."""
        from ai_stock.quant import db_ops
        from ai_stock.quant.mq import enqueue_task, get_mq_backend
        from ai_stock.quant.service import QuantService

        service = QuantService(with_scheduler=False)
        service.start()
        try:
            # 构造一条已过观察期的自选池记录
            db_ops.upsert_optional_stock({"symbol": "600000", "name": "测试股"})
            from ai_stock.quant.db import session_scope
            from ai_stock.quant.db_models import StockPoolOptional

            with session_scope() as s:
                row = s.get(StockPoolOptional, "600000")
                row.observe_expire = datetime.now(timezone.utc) - timedelta(days=1)
                s.commit()

            task_id, created = enqueue_task(
                "risk", "pool_expire", payload={}, idempotent_key="test_pool_expire",
            )
            assert created
            backend = get_mq_backend()
            deadline = time.monotonic() + 15
            status = ""
            while time.monotonic() < deadline:
                meta = backend.get_meta(task_id)
                status = (meta or {}).get("status", "")
                if status == "done":
                    break
                time.sleep(0.3)
            assert status == "done"
            assert db_ops.get_optional_stock("600000")["status"] == "expired"
        finally:
            service.stop()


@pytest.mark.unit
class TestSchedulerTriggers:
    def test_aligned_window(self):
        from ai_stock.quant.scheduler import _aligned_hhmm

        assert _aligned_hhmm(datetime(2026, 8, 28, 9, 45), 30) == "0930"
        assert _aligned_hhmm(datetime(2026, 8, 28, 10, 5), 10) == "1000"

    def test_buy_scan_idempotent(self, quant_env):
        from ai_stock.quant.scheduler import trigger_buy_scan

        now = datetime(2026, 8, 28, 9, 45)  # 交易时段内
        first = trigger_buy_scan(now=now, force=True)
        assert first is not None
        # 同一 30 分钟槽位重复触发直接丢弃 (§5.3 调度幂等)
        assert trigger_buy_scan(now=datetime(2026, 8, 28, 9, 50), force=True) is None
        # 下一个槽位重新入队
        assert trigger_buy_scan(now=datetime(2026, 8, 28, 10, 0), force=True) is not None

    def test_selection_trigger_idempotent(self, quant_env):
        from ai_stock.quant.scheduler import trigger_selection

        now = datetime(2026, 8, 28, 8, 0)
        assert trigger_selection(now=now, force=True) is not None
        assert trigger_selection(now=now, force=True) is None

    def test_trading_session_guard(self, quant_env, monkeypatch):
        """非交易时段/非交易日不入队 (不强制时)."""
        from ai_stock.quant import scheduler

        monkeypatch.setattr(scheduler, "is_trading_time", lambda dt=None: False)
        assert scheduler.trigger_buy_scan(now=datetime(2026, 8, 28, 9, 0)) is None
        monkeypatch.setattr(scheduler, "is_trading_day", lambda dt=None: False)
        assert scheduler.trigger_selection(now=datetime(2026, 8, 28, 8, 0)) is None

    def test_fallback_jobs_cover_all_triggers(self):
        from ai_stock.quant.config import SELECTION_SCHEDULE
        from ai_stock.quant.scheduler import QuantScheduler

        jobs = QuantScheduler()._fallback_jobs()
        names = {j["name"] for j in jobs}
        assert len([j for j in jobs if j["kind"] == "daily"]) >= len(SELECTION_SCHEDULE) + 3
        assert {"buy_scan", "hold_scan", "risk_scan", "flow_timeout_check"} <= names

    def test_maintenance_handlers_registered(self, quant_env):
        from ai_stock.quant.config import RISK_QUEUE
        from ai_stock.quant.scheduler import build_maintenance_handlers
        from ai_stock.quant.agents import build_handlers

        handlers = build_handlers()
        handlers.setdefault(RISK_QUEUE, {}).update(build_maintenance_handlers())
        assert "daily_report" in handlers[RISK_QUEUE]
        assert "flow_timeout_check" in handlers[RISK_QUEUE]
        assert "risk_scan" in handlers[RISK_QUEUE]


@pytest.mark.unit
class TestRulePipeline:
    def test_seed_rules_pass_own_test_cases(self, quant_env):
        from ai_stock.quant.rules_engine import run_rule_test_cases, seed_rules_and_cases

        seed_rules_and_cases()
        passed, results = run_rule_test_cases("stop_loss_5pct")
        assert passed, results

    def test_submit_without_cases_rejected(self, quant_env):
        from ai_stock.quant.rules_engine import submit_rule_candidate

        outcome = submit_rule_candidate({
            "rule_id": "no_case_rule",
            "condition": "x > 1",
            "action": "sell_all",
        })
        # 上线流水线强制单测: 无用例直接失败 (§10.2 不可跳过)
        assert outcome["status"] == "test_failed"


@pytest.mark.unit
class TestCatchUpSelection:
    """启动/唤醒补跑: 停机/休眠错过的选股槽位在服务启动时补发一次."""

    def _patch_calendar(self, monkeypatch, trading_day=True, trade_date="2026-08-31"):
        from ai_stock.quant import scheduler

        monkeypatch.setattr(scheduler, "is_trading_day", lambda dt=None: trading_day)
        monkeypatch.setattr(scheduler, "get_trade_date", lambda: trade_date)

    def test_catch_up_fires_when_slot_missing(self, quant_env, monkeypatch):
        """今日行业榜未产出且无在跑流程 → 首槽过点后补发一次."""
        from ai_stock.quant import scheduler

        self._patch_calendar(monkeypatch)
        task_id = scheduler.catch_up_selection(now=datetime(2026, 8, 31, 12, 30))
        assert task_id is not None

    def test_catch_up_skipped_when_board_exists(self, quant_env, monkeypatch):
        """今日行业榜已产出 (cron/手动) → 不重复补发."""
        from ai_stock.quant import db_ops, scheduler

        self._patch_calendar(monkeypatch)
        monkeypatch.setattr(
            db_ops, "get_industry_board_by_date",
            lambda date_str: {"date": date_str, "rows": []},
        )
        assert scheduler.catch_up_selection(now=datetime(2026, 8, 31, 12, 30)) is None

    def test_catch_up_skipped_when_running_flow_exists(self, quant_env, monkeypatch):
        """已有选股 flow 在跑 (如超时尚未巡检) → 不并发补发."""
        from ai_stock.quant import db_ops, scheduler

        self._patch_calendar(monkeypatch)
        db_ops.create_flow("selection_2026-08-31_0700", "selection", 3600)  # running
        assert scheduler.catch_up_selection(now=datetime(2026, 8, 31, 12, 30)) is None

    def test_catch_up_skipped_before_any_slot(self, quant_env, monkeypatch):
        """当前时刻早于首个槽位 → 无已过槽位, 不补发."""
        from ai_stock.quant import scheduler

        self._patch_calendar(monkeypatch)
        assert scheduler.catch_up_selection(now=datetime(2026, 8, 31, 6, 30)) is None

    def test_catch_up_skipped_on_non_trading_day(self, quant_env, monkeypatch):
        from ai_stock.quant import scheduler

        self._patch_calendar(monkeypatch, trading_day=False)
        assert scheduler.catch_up_selection(now=datetime(2026, 8, 30, 12, 30)) is None

    def test_catch_up_idempotent(self, quant_env, monkeypatch):
        """同一时刻重复补发被幂等 key 去重."""
        from ai_stock.quant import scheduler

        self._patch_calendar(monkeypatch)
        now = datetime(2026, 8, 31, 12, 30)
        assert scheduler.catch_up_selection(now=now) is not None
        # 第二次: 榜仍未产出且无在跑 flow → 仍尝试入队, 但同一 HHMM 的
        # 幂等 key 已存在, enqueue 拒绝重复 → 返回 None (10 分钟巡检周期安全)
        assert scheduler.catch_up_selection(now=now) is None


@pytest.mark.unit
class TestQuantLLMTimeoutGuard:
    """quant LLM 必须带超时/重试: 无超时的 invoke 曾把选股步拖到 3600s 熔断."""

    def test_create_quant_llm_injects_timeout_and_retries(self, monkeypatch):
        from ai_stock.quant import llm_helper
        from ai_stock.quant.config import LLM_MAX_RETRIES, LLM_REQUEST_TIMEOUT

        captured: list[dict] = []

        class _FakeClient:
            def get_llm(self):
                return object()

        def fake_factory(provider, model, base_url=None, **kwargs):
            captured.append(kwargs)
            return _FakeClient()

        monkeypatch.setattr(
            "ai_stock.llm_clients.factory.create_llm_client", fake_factory,
        )
        llm_helper.create_quant_llm(
            {"quick_think_llm": "q-model", "deep_think_llm": "d-model"},
        )
        # quick + deep 两个模型均注入超时保护
        assert len(captured) == 2
        for kw in captured:
            assert kw["timeout"] == LLM_REQUEST_TIMEOUT
            assert kw["max_retries"] == LLM_MAX_RETRIES
