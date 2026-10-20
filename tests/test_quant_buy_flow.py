"""买入链路 P0/P1 修复的回归测试。

对应 2026-10-20 线上「量化板块全天零交易」的排查结论：

- **P0-1 评估数量 ≠ 开仓数量**：``handle_buy_scan`` 原来用 ``MAX_HOLDING_COUNT``
  截断扫描池（3），池内 22 只时有 19 只全年不被评估。当天真正触发信号的 4 只
  （华明装备/中航西飞/威海广泰/中航机载）dyn 排名 6~22，全部漏掉。
- **P0-2 盘中拿的是昨天的日线**：日线接口盘中不返回未完成 bar，688002 全天
  24 次扫描 close 恒为 157.79，实际收 160.95 → 信号天然滞后一天。
- **P1-3 二次验证结构性死锁**：判据问「利空是否消除」，而清单里是 ROE/PE/换手
  这类不可能"消除"的结构性事实，10-02 之后 27 次进入 0 次通过。
  ``risk_reward_ratio`` 存的是「1:1.4」这种自由文本，从未解析成数值，"不达标
  拒绝"实际上没生效。
- **P1-4 同票无冷却**：每 10 分钟重复深度验证同一批被拒的票。

全部用 Mock 数据服务与假 Orchestrator，不碰外部行情与真实 LLM。
"""

from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# 公共 fixture / 工具
# ---------------------------------------------------------------------------


@pytest.fixture()
def quant_env(tmp_path, monkeypatch):
    from ai_stock.quant import db, mq

    db_url = f"sqlite:///{tmp_path / 'quant_buy_test.db'}"
    monkeypatch.setenv("QUANT_DB_URL", db_url)
    monkeypatch.setattr(mq, "MQ_BACKEND", "sqlite")
    db.reset_engine(db_url)
    mq.reset_mq_backend()
    db.init_quant_db()
    yield db_url
    mq.reset_mq_backend()


class _FakeOrchestrator:
    def __init__(self):
        self.started: list[dict] = []

    def start_flow(self, flow_type, flow_id=None, data=None, **kwargs):
        self.started.append({"flow_type": flow_type, "flow_id": flow_id, "data": data})
        return flow_id


def _pool(n: int = 8) -> list[dict]:
    """按 dyn 降序的自选池（symbol 越大 dyn 越低，模拟真实分布）。"""
    return [
        {
            "symbol": f"60000{i}",
            "name": f"票{i}",
            "dyn_confidence": round(0.9 - i * 0.05, 2),
            "stage_batch": "BODY",
            "reason": "测试",
        }
        for i in range(n)
    ]


def _scan(task_window: str = "202610201000"):
    return {"task_id": "t", "payload": {"window": task_window}}


# ---------------------------------------------------------------------------
# P0-1: 评估整池，开仓数才受持仓上限约束
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuyScanCoverage:
    def _patch(self, monkeypatch, pool, triggered: set[str]):
        from ai_stock.quant.agents import buy as buy_mod

        orch = _FakeOrchestrator()
        monkeypatch.setattr(buy_mod, "db_ops", _FakeDbOps(pool))
        monkeypatch.setattr(buy_mod, "get_orchestrator", lambda: orch)
        monkeypatch.setattr(buy_mod, "get_data_service", lambda: object())
        monkeypatch.setattr(buy_mod, "hard_collapse_reason", lambda data, sym, **kw: "")
        monkeypatch.setattr(
            buy_mod, "evaluate_tech_signals",
            lambda data, sym: {
                "available": True,
                "passed": sym in triggered,
                "channel": "standard" if sym in triggered else "none",
                "signals": {},
                "close": 10.0,
                "bar_date": "2026-10-20",
                "provisional": True,
                "reason": "测试",
            },
        )
        monkeypatch.setattr(buy_mod, "_running_buy_symbols", lambda: set())
        monkeypatch.setattr(buy_mod, "_cooling_symbols", lambda: set())
        return buy_mod, orch

    def test_stock_beyond_free_slots_is_evaluated(self, monkeypatch):
        """持仓 2 只（只剩 1 个仓位），第 6 名的票触发也要进流程。

        原实现会在第 2 只票之后就 break，第 6 名永远看不到。
        """
        from ai_stock.quant.agents import buy as buy_mod

        pool = _pool(8)
        target = pool[5]["symbol"]
        mod, orch = self._patch(monkeypatch, pool, {target})
        monkeypatch.setattr(
            mod.db_ops, "get_holdings", lambda: [{"symbol": "000001"}, {"symbol": "000002"}],
            raising=False,
        )
        result = mod.handle_buy_scan(_scan())
        assert target in result["started_flows"]
        assert result["evaluated"] == len(pool)          # 整池都评估了
        assert result["free_slots"] == 1

    def test_no_signal_stocks_do_not_start_flows(self, monkeypatch):
        """预筛不通过的票不启动 flow（省掉一次 LLM 崩塌复核）。"""
        pool = _pool(5)
        mod, orch = self._patch(monkeypatch, pool, {pool[0]["symbol"]})
        result = mod.handle_buy_scan(_scan())
        assert result["started_flows"] == [pool[0]["symbol"]]
        assert len(orch.started) == 1
        assert len(result["no_signal"]) == 4

    def test_full_position_skips_scan(self, monkeypatch):
        pool = _pool(4)
        mod, orch = self._patch(monkeypatch, pool, {pool[0]["symbol"]})
        monkeypatch.setattr(
            mod.db_ops, "get_holdings",
            lambda: [{"symbol": f"00000{i}"} for i in range(3)],
            raising=False,
        )
        result = mod.handle_buy_scan(_scan())
        assert result["started_flows"] == []
        assert "持仓已满" in result["skipped"]
        assert orch.started == []

    def test_started_flows_bounded_by_slots_plus_overflow(self, monkeypatch):
        """全部触发也不能无限起流程：空仓位 + 溢出为上限。"""
        from ai_stock.quant.config import BUY_SCAN_FLOW_OVERFLOW, MAX_HOLDING_COUNT

        pool = _pool(8)
        mod, orch = self._patch(monkeypatch, pool, {p["symbol"] for p in pool})
        result = mod.handle_buy_scan(_scan())
        assert len(result["started_flows"]) == MAX_HOLDING_COUNT + BUY_SCAN_FLOW_OVERFLOW
        assert result["deferred"]                    # 其余进入 deferred，不是丢弃

    def test_cooling_symbols_are_skipped(self, monkeypatch):
        pool = _pool(3)
        mod, orch = self._patch(monkeypatch, pool, {pool[0]["symbol"]})
        monkeypatch.setattr(mod, "_cooling_symbols", lambda: {pool[0]["symbol"]})
        result = mod.handle_buy_scan(_scan())
        assert result["started_flows"] == []
        assert pool[0]["symbol"] in result["cooling"]

    def test_prescreen_collapse_removes_from_pool(self, monkeypatch):
        pool = _pool(3)
        mod, orch = self._patch(monkeypatch, pool, set())
        removed: list[tuple] = []
        monkeypatch.setattr(
            mod, "hard_collapse_reason", lambda data, sym, **kw: "跌破年线 5%",
        )
        monkeypatch.setattr(
            mod.db_ops, "update_optional_status",
            lambda symbol, status, remove_reason="": removed.append((symbol, status)),
            raising=False,
        )
        result = mod.handle_buy_scan(_scan())
        assert len(result["removed"]) == 3
        assert all(status == "removed" for _, status in removed)


class _FakeDbOps:
    """只实现 buy_scan 用到的查询（避免整个 quant 库依赖）。"""

    def __init__(self, pool: list[dict]):
        self._pool = pool

    def get_holdings(self):
        return []

    def get_optional_pool(self, status="active"):
        return list(self._pool)

    def update_optional_status(self, symbol, status, remove_reason=""):
        return None


@pytest.mark.unit
def test_prescreen_skips_tail_check_to_halve_fetches(monkeypatch):
    """预筛不查鱼尾：鱼尾走另一个 K 线缓存键，整池预筛会翻倍取数。"""
    from ai_stock.quant.agents import buy as buy_mod

    calls: list[str] = []
    monkeypatch.setattr(
        buy_mod, "is_tail_phase",
        lambda data, symbol: calls.append(symbol) or True,
    )

    class _Data:
        def get_stock_indicators(self, symbol):
            return {"close_series": [10.0], "close_200_sma": 5.0}

    data = _Data()
    assert buy_mod.hard_collapse_reason(data, "600000", check_tail=False) == ""
    assert calls == []                       # 预筛没触发鱼尾取数
    assert "鱼尾" in buy_mod.hard_collapse_reason(data, "600000")
    assert calls == ["600000"]               # flow 内仍然查


# ---------------------------------------------------------------------------
# P0-2: 实时价拼 provisional bar
# ---------------------------------------------------------------------------


def _daily_df(days: int = 40):
    import pandas as pd

    dates = pd.bdate_range("2026-08-20", periods=days)
    return pd.DataFrame({
        "Date": dates,
        "Open": [100.0 + i * 0.2 for i in range(days)],
        "High": [101.0 + i * 0.2 for i in range(days)],
        "Low": [99.0 + i * 0.2 for i in range(days)],
        "Close": [100.5 + i * 0.2 for i in range(days)],
        "Volume": [1_000_000.0] * days,
    })


@pytest.mark.unit
class TestProvisionalBar:
    TZ = timezone(timedelta(hours=8))

    def test_session_progress_covers_lunch_and_close(self):
        from ai_stock.quant.data_service import _session_progress

        at = lambda h, m: datetime(2026, 10, 20, h, m, tzinfo=self.TZ)
        assert _session_progress(at(9, 0)) == 0.0
        assert _session_progress(at(12, 0)) == pytest.approx(0.5, abs=0.01)
        assert _session_progress(at(15, 30)) == 1.0
        assert 0.9 < _session_progress(at(14, 50)) < 1.0

    def test_appends_today_bar_from_quote(self):
        from ai_stock.quant.data_service import _merge_provisional_bar

        df = _daily_df()
        last_close = float(df["Close"].iloc[-1])
        quote = {
            "price": last_close * 1.02, "prev_close": last_close,
            "open": last_close, "high": last_close * 1.03, "low": last_close * 0.99,
            "turnover_pct": 2.0, "float_mcap": 200e8,
        }
        now = datetime(2026, 10, 20, 14, 0, tzinfo=self.TZ)
        out = _merge_provisional_bar(df.copy(), quote, now=now)
        assert len(out) == len(df) + 1
        assert str(out["Date"].iloc[-1])[:10] == "2026-10-20"
        assert out["Close"].iloc[-1] == pytest.approx(round(last_close * 1.02, 2))
        # 成交量按已过时段（14:00 → 75%）折算，量比才有可比性
        assert out["Volume"].iloc[-1] > 1_000_000

    def test_skips_when_prev_close_mismatch(self):
        """日 K 末根与实时行情的昨收对不上 → 不拼（宁可少一根也不拼错）。"""
        from ai_stock.quant.data_service import _merge_provisional_bar

        df = _daily_df()
        last_close = float(df["Close"].iloc[-1])
        quote = {"price": last_close, "prev_close": last_close * 0.8}
        now = datetime(2026, 10, 20, 14, 0, tzinfo=self.TZ)
        assert len(_merge_provisional_bar(df.copy(), quote, now=now)) == len(df)

    def test_updates_in_place_when_today_bar_exists(self):
        import pandas as pd

        from ai_stock.quant.data_service import _merge_provisional_bar

        df = _daily_df()
        last_close = float(df["Close"].iloc[-1])
        df = pd.concat([df, pd.DataFrame([{
            "Date": pd.Timestamp("2026-10-20"), "Open": last_close,
            "High": last_close * 1.01, "Low": last_close * 0.995,
            "Close": last_close, "Volume": 3_000_000.0,
        }])], ignore_index=True)
        quote = {
            "price": last_close * 1.02, "prev_close": last_close,
            "open": last_close, "high": last_close * 1.025, "low": last_close * 1.0,
            "turnover_pct": 2.0, "float_mcap": 200e8,
        }
        now = datetime(2026, 10, 20, 14, 0, tzinfo=self.TZ)
        out = _merge_provisional_bar(df.copy(), quote, now=now)
        assert len(out) == len(df)                 # 不新增行
        assert out["Close"].iloc[-1] == pytest.approx(round(last_close * 1.02, 2))
        assert out["Volume"].iloc[-1] > 3_000_000  # 量随盘中推进而放大

    def test_indicators_carry_bar_observability(self, monkeypatch):
        """指标里带上 bar_date / provisional，排查时不用翻缓存表。"""
        from ai_stock.quant import data_service as ds_mod

        service = ds_mod.DataService.__new__(ds_mod.DataService)  # 绕过 __init__
        monkeypatch.setattr(
            service, "get_ohlcv",
            lambda symbol, lookback_days=120, live=False: _daily_df(),
        )
        ind = service.get_stock_indicators("600000")
        # 表里最后一根是 8~10 月的工作日，不是"今天"，所以 provisional=False
        assert ind["bar_date"]
        assert ind["provisional"] is False


# ---------------------------------------------------------------------------
# P1-3: 二次验证判据 + 风险收益比校验
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVerificationCriteria:
    def _handle(self, monkeypatch, **report_fields):
        from ai_stock.quant.agents import buy as buy_mod
        from ai_stock.quant.llm_helper import QuantLLM

        report = buy_mod.VerificationReport(**report_fields)
        monkeypatch.setattr(
            buy_mod, "structured_invoke", lambda llm, schema, prompt, **kw: report,
        )
        agent = buy_mod.SecondVerificationAgent(data_service=_FakeMarket())
        agent._llm_holder = QuantLLM(llm_quick=object(), llm_deep=object())
        return agent.handle({
            "task_id": "t", "payload": {
                "flow_id": "f", "flow_type": "buy", "step": "second_verification",
                "context": {"symbol": "600000", "pool": {}},
            },
        })

    def test_unresolved_but_not_worsened_passes(self, quant_env, monkeypatch):
        """结构性利空「未消除但没有恶化」应当通过 —— 这是 27 连拒的症结。"""
        result = self._handle(
            monkeypatch, passed=True, reason="利空未消除但无恶化",
            major_bear_resolved=False, bear_risk_level="medium",
            bear_worsened=False, risk_reward=2.0,
        )
        assert result["passed"] is True
        assert result["reject_stage"] == ""

    def test_worsened_bear_blocks(self, quant_env, monkeypatch):
        result = self._handle(
            monkeypatch, passed=True, reason="业绩下修",
            major_bear_resolved=False, bear_risk_level="medium",
            bear_worsened=True, risk_reward=2.0,
        )
        assert result["passed"] is False
        assert result["reject_stage"] == "bear_deteriorated"

    def test_weak_risk_reward_rejected(self, quant_env, monkeypatch):
        result = self._handle(
            monkeypatch, passed=True, reason="ok",
            bear_risk_level="low", risk_reward_ratio="1:1.1",
        )
        assert result["passed"] is False
        assert result["reject_stage"] == "weak_risk_reward"
        assert result["risk_reward"] == pytest.approx(1.1)

    def test_text_ratio_is_parsed(self, quant_env, monkeypatch):
        """LLM 写「风险 1 : 收益 2.6」这类文本也要能校验，不再只看自由文本。"""
        result = self._handle(
            monkeypatch, passed=True, reason="ok",
            bear_risk_level="low", risk_reward_ratio="1:2.6",
        )
        assert result["risk_reward"] == pytest.approx(2.6)
        assert result["passed"] is True


@pytest.mark.unit
class TestRiskRewardParser:
    @pytest.mark.parametrize("text,expected", [
        ("1:1.4", 1.4),      # 风险:收益
        ("2.6:1", 2.6),      # 收益:风险
        ("约 2.6", 2.6),
        ("", 0.0),
        ("不确定", 0.0),
    ])
    def test_parse(self, text, expected):
        from ai_stock.quant.agents.buy import VerificationReport, _parse_risk_reward

        parsed = _parse_risk_reward(VerificationReport(risk_reward_ratio=text))
        assert parsed == pytest.approx(expected)


class _FakeMarket:
    """二次验证用到的数据接口最小实现。"""

    def get_stock_news(self, symbol, hours=72):
        return []

    def get_fundamentals_text(self, symbol):
        return ""

    def get_realtime_quote(self, symbol):
        return {"name": "测试", "price": 10.0}


# ---------------------------------------------------------------------------
# 交易时段闸门：flow 异步执行会跨到午休/收盘之后
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTradingSessionGate:
    SNAP = {
        "available_cash": 1_000_000.0, "market_value": 0.0,
        "total_assets": 1_000_000.0, "drawdown_pct": 0.0, "daily_pnl": 0.0,
    }

    def _order(self):
        from ai_stock.quant.broker import OrderRequest, OrderSide, OrderType

        return OrderRequest(
            symbol="600000", side=OrderSide.BUY, quantity=100,
            order_type=OrderType.MARKET,
        )

    def _check(self, monkeypatch, when):
        from ai_stock.quant import db_ops, risk_control

        class _Svc:
            def get_realtime_quote(self, symbol):
                return {"price": 10.0, "name": "测试", "limit_up": 11.0, "limit_down": 9.0}

        # 闸门用例只看时段判定：关掉大盘择时门并伪造行情，避免走真实网络
        db_ops.set_config_value("market_timing_enable", "0")
        monkeypatch.setattr(risk_control, "get_data_service", lambda: _Svc())
        monkeypatch.setattr(risk_control, "_now_market", lambda: when)
        return risk_control.pre_trade_check(self._order(), dict(self.SNAP))

    def test_rejects_after_close(self, quant_env, monkeypatch):
        """21:00 走到建仓步骤 → 不得下单（原来没有这道闸门）。"""
        from datetime import datetime, timedelta, timezone

        when = datetime(2026, 10, 20, 21, 0, tzinfo=timezone(timedelta(hours=8)))
        result = self._check(monkeypatch, when)
        assert result.ok is False
        assert "非交易时段" in result.reason

    def test_rejects_during_lunch_break(self, quant_env, monkeypatch):
        from datetime import datetime, timedelta, timezone

        when = datetime(2026, 10, 20, 12, 0, tzinfo=timezone(timedelta(hours=8)))
        assert self._check(monkeypatch, when).ok is False

    def test_passes_gate_inside_session(self, quant_env, monkeypatch):
        """盘中不应被闸门拦下（后续可能因其它规则拒绝，但不是时段原因）。"""
        from datetime import datetime, timedelta, timezone

        when = datetime(2026, 10, 20, 10, 30, tzinfo=timezone(timedelta(hours=8)))
        assert "非交易时段" not in self._check(monkeypatch, when).reason

    def test_gate_can_be_disabled(self, quant_env, monkeypatch):
        from datetime import datetime, timedelta, timezone

        from ai_stock.quant import db_ops

        db_ops.set_config_value("trading_session_guard_enable", "0")
        when = datetime(2026, 10, 20, 21, 0, tzinfo=timezone(timedelta(hours=8)))
        assert "非交易时段" not in self._check(monkeypatch, when).reason
