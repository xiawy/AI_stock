"""主题雷达 + 动态置信度 + 清理官 增量改造单元测试 (旁路注入增强).

覆盖计划 §8 全部验收点:
- ThemeRadarAgent: 空/异常降级不产出 alerts, 命中聚类按强度降序
- IndustryScanAgent._collect_candidates: 雷达空不注入 / 名称命中注入 / 反查注入
- StockSelectionAgent: dyn_confidence 加权计算 + stage_batch(HEAD/BODY) 入池,
  原 confidence(0-10) 语义不变
- ConfidenceMaintainAgent: 增强 / 衰减 / 衰竭踢出 / 鱼尾标记
- WatchlistKickerAgent: 置信度衰竭踢出 / 鱼尾踢出 / 放行 / 已不在池
- LogicCollapseAgent: 鱼尾硬拦截 (数据驱动, 不经 LLM)
- market_utils.is_tail_phase: 生产常量下真·高位放量滞涨触发 + 非鱼尾形态保守 False
- TechSignalAgent: _check_breakout 判定 + 突破通道 (标准未过才走突破)
- PositionOpenAgent: 动态仓位系数 (含字段缺失退化为原 20%)
- db._migrate_pool_radar_schema: 幂等 + 旧行默认值 (dyn=0.5, stage=BODY)
- FSM: buy/selection 步骤序列 + 新 handler 注册

全部走 SQLite 降级队列 + 临时 DB + Mock 数据服务, 不依赖外部行情/真实 LLM.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest


@pytest.fixture()
def quant_env(tmp_path, monkeypatch):
    """独立临时 quant DB + SQLite 队列后端 (与 test_selection_agents 同口径)."""
    from ai_stock.quant import db, mq

    db_url = f"sqlite:///{tmp_path / 'quant_radar_test.db'}"
    monkeypatch.setenv("QUANT_DB_URL", db_url)
    monkeypatch.setattr(mq, "MQ_BACKEND", "sqlite")
    db.reset_engine(db_url)
    mq.reset_mq_backend()
    db.init_quant_db()
    yield db_url
    mq.reset_mq_backend()


class FakeDataService:
    """Mock 数据服务: 覆盖雷达/置信度/买入集群用到的接口."""

    def __init__(self):
        self.boards: list[dict] = []
        self.industry_stocks: dict[str, list[dict]] = {}
        self.quotes: dict[str, dict] = {}
        self.fundamentals: dict[str, str] = {}
        self.anomalies: dict[str, list[str]] = {}
        self.industry_code_map: dict[str, str] = {}
        self.ohlcv: dict[str, object] = {}
        self.indicators: dict[str, dict] = {}

    # -- 选股集群 --
    def get_hot_news(self, days=3):
        return []

    def get_all_industries(self):
        return self.boards

    def get_industry_detail(self, industry_code):
        for b in self.boards:
            if b.get("code") == industry_code:
                return b
        return None

    def get_industry_stocks(self, industry_code, top_n=20, sort_by="amount"):
        return self.industry_stocks.get(industry_code, [])[:top_n]

    def get_realtime_quote(self, symbol):
        return self.quotes.get(symbol)

    def get_fundamentals_text(self, symbol):
        return self.fundamentals.get(symbol, "")

    def get_stock_news(self, symbol, hours=72):
        return []

    def get_limit_up_stocks(self, days=1):
        return []

    # -- 主题雷达 --
    def scan_micro_anomalies(self):
        return self.anomalies

    def get_stock_industry_code(self, code):
        return self.industry_code_map.get(code)

    # -- 鱼尾检测 / 买入技术面 --
    def get_ohlcv(self, symbol, days=15):
        return self.ohlcv.get(symbol)

    def get_stock_indicators(self, symbol):
        return self.indicators.get(symbol)


def _make_agent(agent_cls, data, llm_available: bool = False):
    from ai_stock.quant.llm_helper import QuantLLM

    agent = agent_cls(data_service=data)
    if llm_available:
        agent._llm_holder = QuantLLM(llm_quick=object(), llm_deep=object())
    else:
        agent._llm_holder = QuantLLM(None, None)
    return agent


def _sel_task(context: dict | None = None) -> dict:
    return {
        "task_id": "t1",
        "payload": {
            "flow_id": "sel_test_flow", "flow_type": "selection",
            "step": "", "context": context or {},
        },
    }


def _buy_task(context: dict | None = None) -> dict:
    return {
        "task_id": "t1",
        "payload": {
            "flow_id": "buy_test_flow", "flow_type": "buy",
            "step": "", "context": context or {},
        },
    }


def _upsert(symbol, *, name="N", industry="I", confidence=7.0,
            dyn=0.5, stage="BODY", theme=""):
    """写入一条自选池记录 (走真实 db_ops, 落 SQLite)."""
    from ai_stock.quant import db_ops

    db_ops.upsert_optional_stock({
        "symbol": symbol, "name": name, "industry": industry,
        "reason": "r", "bull_factors": [], "bear_factors": [],
        "stage_judgement": "", "rise_trigger": "", "risk_tags": [],
        "confidence": confidence, "dyn_confidence": dyn,
        "stage_batch": stage, "radar_theme": theme,
    })


# 真·鱼尾样本 (30 根): 前 25 根从 10.0 稳步拉升到 13.84 (+38%, 鱼身已成),
# 末 5 根高位滞涨 (近 5 日 +1.7% < 3%), 末根放量, 现价贴近窗口最高。
# 用 "生产常量" 即应判定为鱼尾 (无需 monkeypatch 任何阈值)。
_RALLY_CLOSE = [round(10.0 + i * 0.16, 2) for i in range(25)]   # 10.00 .. 13.84
_TOP_CLOSE = [13.88, 13.93, 13.98, 14.03, 14.08]                # 高位滞涨
_TAIL_CLOSE = _RALLY_CLOSE + _TOP_CLOSE                          # 30 根
_TAIL_HIGH = [round(c * 1.005, 2) for c in _TAIL_CLOSE]
_TAIL_VOL_SURGE = [1000] * 29 + [3000]
_TAIL_VOL_FLAT = [1000] * 30


def _ohlcv_df(close, high, vol) -> "pd.DataFrame":
    n = len(close)
    return pd.DataFrame({
        "Date": pd.date_range("2026-01-01", periods=n),
        "Open": close,
        "High": high,
        "Low": [c * 0.99 for c in close],
        "Close": close,
        "Volume": vol,
    })


# ---------------------------------------------------------------------------
# ThemeRadarAgent (主题雷达)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestThemeRadar:
    def test_empty_scan_degrades_to_empty_alerts(self, quant_env):
        from ai_stock.quant.agents.radar import ThemeRadarAgent

        data = FakeDataService()
        data.anomalies = {}
        result = _make_agent(ThemeRadarAgent, data).handle(_sel_task())
        assert result["decision"] == "empty"
        assert result["radar_alerts"] == []
        assert result["themes"] == []

    def test_scan_exception_degrades_to_empty(self, quant_env):
        from ai_stock.quant.agents.radar import ThemeRadarAgent

        data = FakeDataService()

        def _boom():
            raise RuntimeError("数据源挂")

        data.scan_micro_anomalies = _boom  # 实例级覆盖: 模拟数据源异常
        result = _make_agent(ThemeRadarAgent, data).handle(_sel_task())
        assert result["decision"] == "empty"
        assert result["radar_alerts"] == []

    def test_clusters_become_strength_sorted_alerts(self, quant_env):
        from ai_stock.quant.agents.radar import ThemeRadarAgent
        from ai_stock.quant.config import RADAR_STRONG_CLUSTER

        data = FakeDataService()
        data.anomalies = {
            "液冷": ["000004"],
            "AI电源": ["000001", "000002", "000003"],
        }
        result = _make_agent(ThemeRadarAgent, data).handle(_sel_task())
        assert result["decision"] == "ok"
        alerts = result["radar_alerts"]
        # 强度降序: AI电源 (3 家) 排在 液冷 (1 家) 前
        assert [a["theme"] for a in alerts] == ["AI电源", "液冷"]
        assert alerts[0]["codes"] == ["000001", "000002", "000003"]
        assert alerts[0]["strength"] == round(
            min(1.0, 3 / RADAR_STRONG_CLUSTER), 3,
        )
        assert result["themes"] == ["AI电源", "液冷"]


# ---------------------------------------------------------------------------
# IndustryScanAgent._collect_candidates (雷达注入候选池)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRadarCandidateInjection:
    _BOARDS = [
        {"code": "BK1", "name": "AI电源", "change_pct": 5.0,
         "main_net_inflow": 2e8, "up_count": 20, "down_count": 5,
         "top_stock_name": "甲", "top_stock_code": "600001",
         "board_level": "concept"},
        {"code": "BK2", "name": "白酒", "change_pct": 0.3,
         "main_net_inflow": 0.0, "up_count": 5, "down_count": 5,
         "top_stock_name": "", "top_stock_code": "",
         "board_level": "industry"},
    ]

    def test_empty_radar_no_injection(self, quant_env):
        from ai_stock.quant.agents.selection import IndustryScanAgent

        data = FakeDataService()
        data.boards = self._BOARDS
        agent = _make_agent(IndustryScanAgent, data)
        cands = agent._collect_candidates(self._BOARDS, [], [])
        assert cands  # 降级链仍产出候选 (涨幅榜)
        assert all("_radar_theme" not in c for c in cands)

    def test_radar_hits_board_by_name(self, quant_env):
        from ai_stock.quant.agents.selection import IndustryScanAgent

        data = FakeDataService()
        data.boards = self._BOARDS
        agent = _make_agent(IndustryScanAgent, data)
        radar_alerts = [{"theme": "AI电源", "codes": ["600001"], "strength": 0.5}]
        cands = agent._collect_candidates(self._BOARDS, [], radar_alerts)
        by = {c["code"]: c for c in cands}
        assert by["BK1"]["_radar_theme"] == "AI电源"
        assert by["BK1"]["_radar_conf"] == 0.5
        # 未命中的板块不携带雷达标记
        assert "_radar_theme" not in by["BK2"]

    def test_radar_reverse_lookup_by_stock(self, quant_env):
        """主题名不匹配任何板块 → 用簇内个股 get_stock_industry_code 反查注入."""
        from ai_stock.quant.agents.selection import IndustryScanAgent

        boards = [
            {"code": "BK9", "name": "隐秘细分", "change_pct": 0.1,
             "main_net_inflow": 1e7, "up_count": 3, "down_count": 3,
             "top_stock_name": "", "top_stock_code": "",
             "board_level": "concept"},
        ]
        data = FakeDataService()
        data.boards = boards
        data.industry_code_map = {"600555": "BK9"}
        agent = _make_agent(IndustryScanAgent, data)
        radar_alerts = [{"theme": "某新概念", "codes": ["600555"], "strength": 0.6}]
        cands = agent._collect_candidates(boards, [], radar_alerts)
        by = {c["code"]: c for c in cands}
        assert by["BK9"]["_radar_theme"] == "某新概念"
        assert by["BK9"]["_radar_conf"] == 0.6


# ---------------------------------------------------------------------------
# StockSelectionAgent (dyn_confidence / stage_batch 计算与入池)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStockSelectionDynConfidence:
    def _data(self) -> FakeDataService:
        data = FakeDataService()
        data.industry_stocks["BK001"] = [
            {"code": "600519", "name": "龙头股", "turnover_rate": 3.0,
             "volume_ratio": 1.5, "market_cap": 2e11, "amount": 5e9,
             "change_pct": 2.0},
        ]
        data.quotes["600519"] = {"name": "龙头股", "change_pct": 2.0,
                                 "market_cap": 2e11}
        data.fundamentals["600519"] = (
            "Name: 600519\nPE (TTM): 25.3\nPB: 3.1\nROE (%): 18.5"
        )
        return data

    def _run(self, monkeypatch, top_industry: dict):
        from ai_stock.quant.agents import selection
        from ai_stock.quant.agents.selection import (
            StockDeepEval,
            StockDeepEvalList,
            StockSelectionAgent,
        )

        monkeypatch.setattr(
            selection, "structured_invoke",
            lambda llm, schema, prompt, **kw: StockDeepEvalList(stocks=[
                StockDeepEval(code="600519", name="龙头股", score=8.0,
                              elasticity_score=6.0, reason="地位稳固",
                              confidence=8.5),
            ]),
        )
        agent = _make_agent(StockSelectionAgent, self._data(), llm_available=True)
        return agent.handle(_sel_task(
            {"industry_scan": {"top_industries": [top_industry]}},
        ))

    def test_dyn_confidence_and_head_stage_written(self, quant_env, monkeypatch):
        from ai_stock.quant import db_ops

        result = self._run(monkeypatch, {
            "code": "BK001", "name": "半导体", "stage": "验证后期",
            "radar_theme": "AI电源", "radar_conf": 0.8,
        })
        assert result["pool_written"] == 1
        row = db_ops.get_optional_stock("600519")
        # dyn = (8.5/10)*0.6 + 0.8*0.4 = 0.51 + 0.32 = 0.83
        assert row["dyn_confidence"] == pytest.approx(0.83, abs=1e-6)
        # 新主题 (radar_theme) 且当日涨幅 2.0% < 15% → HEAD
        assert row["stage_batch"] == "HEAD"
        assert row["radar_theme"] == "AI电源"
        # 原 confidence (0-10 LLM 分) 语义与写入完全不变
        assert row["confidence"] == 8.5

    def test_no_radar_defaults_to_body(self, quant_env, monkeypatch):
        from ai_stock.quant import db_ops

        result = self._run(monkeypatch, {
            "code": "BK001", "name": "半导体", "stage": "验证后期",
        })
        assert result["pool_written"] == 1
        row = db_ops.get_optional_stock("600519")
        # 无雷达强度 → dyn = (8.5/10)*0.6 = 0.51
        assert row["dyn_confidence"] == pytest.approx(0.51, abs=1e-6)
        assert row["stage_batch"] == "BODY"
        assert row["radar_theme"] == ""


# ---------------------------------------------------------------------------
# ConfidenceMaintainAgent (维护官: 增强/衰减/踢出/鱼尾标记)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConfidenceMaintain:
    def test_reinforce_on_theme_hit(self, quant_env):
        from ai_stock.quant import db_ops
        from ai_stock.quant.agents.radar import ConfidenceMaintainAgent

        _upsert("600001", dyn=0.5, theme="AI电源")
        agent = _make_agent(ConfidenceMaintainAgent, FakeDataService())
        result = agent.handle(_sel_task({"theme_radar": {"themes": ["AI电源"]}}))
        assert result["reinforced"] == 1
        assert result["kicked"] == 0
        row = db_ops.get_optional_stock("600001")
        assert row["dyn_confidence"] == pytest.approx(0.55, abs=1e-6)  # 0.5*1.1

    def test_decay_when_stale(self, quant_env):
        from ai_stock.quant import db_ops
        from ai_stock.quant.agents.radar import ConfidenceMaintainAgent

        _upsert("600002", dyn=0.5, theme="")
        old = datetime.now(timezone.utc) - timedelta(days=10)
        db_ops.update_optional_dynamic("600002", last_confirmed=old)
        agent = _make_agent(ConfidenceMaintainAgent, FakeDataService())
        result = agent.handle(_sel_task({"theme_radar": {"themes": []}}))
        assert result["decayed"] == 1
        row = db_ops.get_optional_stock("600002")
        assert row["dyn_confidence"] == pytest.approx(0.475, abs=1e-6)  # 0.5*0.95

    def test_kick_on_exhaustion(self, quant_env):
        from ai_stock.quant import db_ops
        from ai_stock.quant.agents.radar import ConfidenceMaintainAgent

        _upsert("600003", dyn=0.25, theme="")
        agent = _make_agent(ConfidenceMaintainAgent, FakeDataService())
        result = agent.handle(_sel_task())
        assert result["kicked"] == 1
        row = db_ops.get_optional_stock("600003")
        assert row["status"] == "removed"
        assert row["kicked_reason"] == "置信度衰竭"

    def test_tail_flagged(self, quant_env, monkeypatch):
        from ai_stock.quant import db_ops
        from ai_stock.quant.agents.radar import ConfidenceMaintainAgent

        _upsert("600004", dyn=0.6, theme="")
        monkeypatch.setattr(
            "ai_stock.quant.agents.radar.is_tail_phase", lambda data, symbol: True,
        )
        agent = _make_agent(ConfidenceMaintainAgent, FakeDataService())
        result = agent.handle(_sel_task())
        assert result["tail_flagged"] == 1
        assert result["kicked"] == 0
        row = db_ops.get_optional_stock("600004")
        assert row["stage_batch"] == "TAIL"
        assert row["dyn_confidence"] == pytest.approx(0.6, abs=1e-6)


# ---------------------------------------------------------------------------
# WatchlistKickerAgent (清理官: 买入流程首步硬性过滤)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWatchlistKicker:
    def test_kick_on_low_confidence(self, quant_env):
        from ai_stock.quant import db_ops
        from ai_stock.quant.agents.buy import WatchlistKickerAgent

        _upsert("700001", dyn=0.2, stage="BODY")
        agent = _make_agent(WatchlistKickerAgent, FakeDataService())
        result = agent.handle(_buy_task({"symbol": "700001"}))
        assert result["decision"] == "kicked"
        assert result["abort"] is True
        assert result["kicked_reason"] == "置信度衰竭"
        row = db_ops.get_optional_stock("700001")
        assert row["status"] == "removed"
        assert row["kicked_reason"] == "置信度衰竭"

    def test_kick_on_tail_stage(self, quant_env):
        from ai_stock.quant import db_ops
        from ai_stock.quant.agents.buy import WatchlistKickerAgent

        _upsert("700002", dyn=0.6, stage="TAIL")
        agent = _make_agent(WatchlistKickerAgent, FakeDataService())
        result = agent.handle(_buy_task({"symbol": "700002"}))
        assert result["decision"] == "kicked"
        assert result["kicked_reason"] == "鱼尾"
        assert db_ops.get_optional_stock("700002")["status"] == "removed"

    def test_kick_on_live_tail_detection(self, quant_env, monkeypatch):
        from ai_stock.quant.agents.buy import WatchlistKickerAgent

        _upsert("700003", dyn=0.6, stage="BODY")
        monkeypatch.setattr(
            "ai_stock.quant.agents.buy.is_tail_phase", lambda data, symbol: True,
        )
        agent = _make_agent(WatchlistKickerAgent, FakeDataService())
        result = agent.handle(_buy_task({"symbol": "700003"}))
        assert result["decision"] == "kicked"
        assert result["kicked_reason"] == "鱼尾"

    def test_healthy_stock_passes(self, quant_env):
        from ai_stock.quant.agents.buy import WatchlistKickerAgent

        _upsert("700004", dyn=0.6, stage="BODY")
        agent = _make_agent(WatchlistKickerAgent, FakeDataService())
        result = agent.handle(_buy_task({"symbol": "700004"}))
        assert result["decision"] == "active"
        assert result["passed"] is True
        assert result["dyn_confidence"] == 0.6

    def test_missing_from_pool_aborts(self, quant_env):
        from ai_stock.quant.agents.buy import WatchlistKickerAgent

        agent = _make_agent(WatchlistKickerAgent, FakeDataService())
        result = agent.handle(_buy_task({"symbol": "NOTEXIST"}))
        assert result["decision"] == "gone"
        assert result["abort"] is True


# ---------------------------------------------------------------------------
# LogicCollapseAgent (鱼尾硬拦截)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLogicCollapseTail:
    def test_tail_phase_hard_intercept(self, quant_env, monkeypatch):
        from ai_stock.quant import db_ops
        from ai_stock.quant.agents.buy import LogicCollapseAgent

        _upsert("800001", dyn=0.6, stage="BODY")
        monkeypatch.setattr(
            "ai_stock.quant.agents.buy.is_tail_phase", lambda data, symbol: True,
        )
        agent = _make_agent(LogicCollapseAgent, FakeDataService())
        result = agent.handle(_buy_task({"symbol": "800001", "pool": {}}))
        assert result["decision"] == "collapse_removed"
        assert result["abort"] is True
        assert "鱼尾" in result["remove_reason"]
        row = db_ops.get_optional_stock("800001")
        assert row["status"] == "removed"


# ---------------------------------------------------------------------------
# market_utils.is_tail_phase (鱼尾检测器)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIsTailPhase:
    """鱼尾检测器: 生产常量下真·鱼尾触发, 各非鱼尾形态保守返回 False."""

    def test_tail_fires_with_production_constants(self):
        """关键回归: 不 monkeypatch 任何阈值, 真·高位放量滞涨应被识别为鱼尾.

        (旧版因 MA5 斜率门槛与滞涨门槛数学互斥, 此形态在生产常量下永不触发。)
        """
        from ai_stock.quant.market_utils import is_tail_phase

        data = FakeDataService()
        data.ohlcv["600000"] = _ohlcv_df(_TAIL_CLOSE, _TAIL_HIGH, _TAIL_VOL_SURGE)
        data.quotes["600000"] = {"price": 14.08}  # 换手率不可用 → 退化为量能代理
        assert is_tail_phase(data, "600000") is True

    def test_high_turnover_confirms_tail(self):
        from ai_stock.quant.market_utils import is_tail_phase

        data = FakeDataService()
        data.ohlcv["600000"] = _ohlcv_df(_TAIL_CLOSE, _TAIL_HIGH, _TAIL_VOL_SURGE)
        data.quotes["600000"] = {"price": 14.08, "turnover_rate": 25.0}
        assert is_tail_phase(data, "600000") is True

    def test_none_df_returns_false(self):
        from ai_stock.quant.market_utils import is_tail_phase

        assert is_tail_phase(FakeDataService(), "600000") is False

    def test_insufficient_bars_returns_false(self):
        from ai_stock.quant.market_utils import is_tail_phase

        data = FakeDataService()
        data.ohlcv["600000"] = _ohlcv_df(
            _TAIL_CLOSE[:5], _TAIL_HIGH[:5], [1000] * 5,
        )  # 5 根 < 8
        assert is_tail_phase(data, "600000") is False

    def test_no_volume_surge_returns_false(self):
        from ai_stock.quant.market_utils import is_tail_phase

        data = FakeDataService()
        data.ohlcv["600000"] = _ohlcv_df(_TAIL_CLOSE, _TAIL_HIGH, _TAIL_VOL_FLAT)
        data.quotes["600000"] = {"price": 14.08}
        assert is_tail_phase(data, "600000") is False  # 条件1: 末根量能未放大

    def test_low_turnover_returns_false(self):
        from ai_stock.quant.market_utils import is_tail_phase

        data = FakeDataService()
        data.ohlcv["600000"] = _ohlcv_df(_TAIL_CLOSE, _TAIL_HIGH, _TAIL_VOL_SURGE)
        data.quotes["600000"] = {"price": 14.08, "turnover_rate": 10.0}
        assert is_tail_phase(data, "600000") is False  # 条件1: 换手率 < 20%

    def test_healthy_uptrend_not_tail(self):
        """条件2: 近 5 日仍大涨 (>3%) = 健康上涨, 非滞涨 → 非鱼尾."""
        from ai_stock.quant.market_utils import is_tail_phase

        close = [round(10.0 * (1.015 ** i), 2) for i in range(30)]  # 持续 +1.5%/日
        high = [round(c * 1.005, 2) for c in close]
        data = FakeDataService()
        data.ohlcv["600000"] = _ohlcv_df(close, high, [1000] * 29 + [3000])
        data.quotes["600000"] = {"price": close[-1]}
        assert is_tail_phase(data, "600000") is False

    def test_no_prior_runup_not_tail(self):
        """条件3: 窗口内累计涨幅 <20% (鱼身未成) = 平庸票 → 非鱼尾."""
        from ai_stock.quant.market_utils import is_tail_phase

        close = [round(10.0 + 0.02 * i, 2) for i in range(30)]  # 10.0..10.58 (+5.8%)
        high = [round(c * 1.005, 2) for c in close]
        data = FakeDataService()
        data.ohlcv["600000"] = _ohlcv_df(close, high, [1000] * 29 + [3000])
        data.quotes["600000"] = {"price": close[-1]}
        assert is_tail_phase(data, "600000") is False

    def test_pulled_back_from_high_not_tail(self):
        """条件4: 见顶回落, 现价远离窗口最高 → 非"高位贴顶" → 非鱼尾.

        (此类破位交由止损/破位逻辑处理, 鱼尾检测只抓"仍在顶部的派发"。)
        """
        from ai_stock.quant.market_utils import is_tail_phase

        rally = [10.0, 10.6, 11.2, 11.8, 12.4, 13.0, 13.6, 14.2, 14.8, 15.4, 16.0]
        pull = [15.4, 14.8, 14.2, 13.6, 13.0]
        flat = [12.5] * 14
        close = rally + pull + flat  # 30 根; 现价 12.5 较窗口最高 16 回撤 ~28%
        high = [round(c * 1.005, 2) for c in close]
        data = FakeDataService()
        data.ohlcv["600000"] = _ohlcv_df(close, high, [1000] * 29 + [3000])
        data.quotes["600000"] = {"price": close[-1]}
        assert is_tail_phase(data, "600000") is False


# ---------------------------------------------------------------------------
# TechSignalAgent (主线突破通道)
# ---------------------------------------------------------------------------

# 突破前高形态: 末根放量 + 收盘破前 10 日高点, 但无向上跳空 (走 B 分支)
_BREAKOUT_IND = {
    "close_series": [9.8, 9.9, 10.0, 10.2, 10.5],
    "high_series": [10.0] * 11,
    "low_series": [9.5] * 11,
    "volume_series": [100, 100, 100, 100, 300],
}
# 放量跳空形态: 末根最低 > 前一根最高 (走 A 分支)
_GAP_IND = {
    "close_series": [9.0, 9.0, 9.0, 9.0, 10.6],
    "high_series": [10.0] * 11,
    "low_series": [9.0] * 10 + [10.5],
    "volume_series": [100, 100, 100, 100, 300],
}


@pytest.mark.unit
class TestTechSignalBreakout:
    def test_check_breakout_prior_high(self):
        from ai_stock.quant.agents.buy import TechSignalAgent

        assert TechSignalAgent._check_breakout(_BREAKOUT_IND) is True

    def test_check_breakout_gap(self):
        from ai_stock.quant.agents.buy import TechSignalAgent

        assert TechSignalAgent._check_breakout(_GAP_IND) is True

    def test_check_breakout_requires_volume(self):
        from ai_stock.quant.agents.buy import TechSignalAgent

        no_surge = {**_BREAKOUT_IND, "volume_series": [100, 100, 100, 100, 120]}
        assert TechSignalAgent._check_breakout(no_surge) is False

    def test_check_breakout_insufficient_data(self):
        from ai_stock.quant.agents.buy import TechSignalAgent

        assert TechSignalAgent._check_breakout(
            {"close_series": [10.0], "volume_series": [100]},
        ) is False

    def test_handle_breakout_channel_when_standard_fails(self, quant_env):
        from ai_stock.quant.agents.buy import TechSignalAgent

        _upsert("900001", dyn=0.8, stage="BODY")
        data = FakeDataService()
        data.indicators["900001"] = _BREAKOUT_IND
        agent = _make_agent(TechSignalAgent, data)
        result = agent.handle(_buy_task({"symbol": "900001"}))
        assert result["passed"] is True
        assert result["channel"] == "breakout"

    def test_handle_no_breakout_below_threshold(self, quant_env):
        from ai_stock.quant.agents.buy import TechSignalAgent

        _upsert("900002", dyn=0.5, stage="BODY")  # < CONF_BREAKOUT_THRESHOLD(0.7)
        data = FakeDataService()
        data.indicators["900002"] = _BREAKOUT_IND
        agent = _make_agent(TechSignalAgent, data)
        result = agent.handle(_buy_task({"symbol": "900002"}))
        assert result["passed"] is False
        assert result["channel"] == "none"

    def test_handle_standard_channel_skips_breakout(self, quant_env, monkeypatch):
        """标准三信号命中时走 standard 通道, 不进入突破分支 (标准未过才走突破)."""
        from ai_stock.quant.agents.buy import TechSignalAgent

        _upsert("900003", dyn=0.8, stage="BODY")
        monkeypatch.setattr(
            TechSignalAgent, "_boll_mid_shrink_stop",
            staticmethod(lambda ind: {"triggered": True, "detail": "forced"}),
        )
        data = FakeDataService()
        data.indicators["900003"] = _BREAKOUT_IND
        agent = _make_agent(TechSignalAgent, data)
        result = agent.handle(_buy_task({"symbol": "900003"}))
        assert result["passed"] is True
        assert result["channel"] == "standard"


# ---------------------------------------------------------------------------
# PositionOpenAgent (动态仓位系数)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPositionOpenCoefficient:
    def _run(self, monkeypatch, extra_context: dict) -> int:
        """跑 PositionOpenAgent 至下单前, 捕获按系数换算后的委托数量."""
        from ai_stock.quant.agents import buy as buy_mod

        captured: dict = {}
        monkeypatch.setattr(
            buy_mod, "account_snapshot", lambda: {"available_cash": 1_000_000},
        )

        class _Chk:
            ok = False
            reason = "test-short-circuit"

            def to_dict(self):
                return {}

        def _fake_check(order, snapshot=None):
            captured["qty"] = order.quantity
            return _Chk()

        monkeypatch.setattr(buy_mod, "pre_trade_check", _fake_check)

        data = FakeDataService()
        data.quotes["600000"] = {"price": 10.0, "name": "X"}
        context = {
            "symbol": "600000", "name": "X",
            "second_verification": {
                "passed": True, "reason": "ok", "predicted_path": "",
                "plan": {"stop_loss_pct": 5.0, "take_profit_pct": 15.0},
            },
            **extra_context,
        }
        agent = _make_agent(buy_mod.PositionOpenAgent, data)
        result = agent.handle(_buy_task(context))
        assert result["decision"] == "risk_rejected"  # 在下单前短路
        return captured["qty"]

    @staticmethod
    def _expected_qty(coef: float) -> int:
        from ai_stock.quant.broker import round_lot
        from ai_stock.quant.config import BUY_POSITION_RATIO

        budget = 1_000_000 * BUY_POSITION_RATIO * coef
        est_price = 10.0 * 1.002
        return round_lot(int(budget / est_price / 100) * 100)

    def test_high_conf_body_gets_boost(self, quant_env, monkeypatch):
        from ai_stock.quant.config import POS_COEF_HIGH

        qty = self._run(monkeypatch, {"pool_confidence": 0.9, "pool_stage": "BODY"})
        assert qty == self._expected_qty(POS_COEF_HIGH)

    def test_high_conf_head_not_boosted(self, quant_env, monkeypatch):
        """§6.5 口径: 仅 stage=BODY 享 1.2 系数; HEAD 即便高置信度也落 1.0 档."""
        from ai_stock.quant.config import POS_COEF_MID

        qty = self._run(monkeypatch, {"pool_confidence": 0.9, "pool_stage": "HEAD"})
        assert qty == self._expected_qty(POS_COEF_MID)

    def test_mid_conf_standard(self, quant_env, monkeypatch):
        from ai_stock.quant.config import POS_COEF_MID

        qty = self._run(monkeypatch, {"pool_confidence": 0.5, "pool_stage": "BODY"})
        assert qty == self._expected_qty(POS_COEF_MID)

    def test_low_conf_halved(self, quant_env, monkeypatch):
        from ai_stock.quant.config import POS_COEF_LOW

        qty = self._run(monkeypatch, {"pool_confidence": 0.4, "pool_stage": "BODY"})
        assert qty == self._expected_qty(POS_COEF_LOW)

    def test_missing_field_degrades_to_original_ratio(self, quant_env, monkeypatch):
        """字段缺失 (pool_confidence 不在 context) → coef=1.0 → 退化为原 20% 仓位."""
        from ai_stock.quant.config import POS_COEF_MID

        qty = self._run(monkeypatch, {})
        assert qty == self._expected_qty(POS_COEF_MID)


# ---------------------------------------------------------------------------
# db._migrate_pool_radar_schema (迁移幂等 + 旧行默认值)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPoolRadarMigration:
    def test_migration_adds_columns_with_defaults_idempotent(self, tmp_path):
        from sqlalchemy import create_engine, inspect, text

        from ai_stock.quant import db

        engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE quant_stock_pool_optional ("
                "symbol VARCHAR(16) PRIMARY KEY, name VARCHAR(64), "
                "status VARCHAR(16), confidence FLOAT)"
            ))
            conn.execute(text(
                "INSERT INTO quant_stock_pool_optional "
                "(symbol, name, status, confidence) "
                "VALUES ('OLD001', '旧行', 'active', 7.5)"
            ))
        cols_before = {
            c["name"]
            for c in inspect(engine).get_columns("quant_stock_pool_optional")
        }
        assert "dyn_confidence" not in cols_before

        db._migrate_pool_radar_schema(engine)
        db._migrate_pool_radar_schema(engine)  # 幂等: 再跑不报错/不重复加列

        cols = {
            c["name"]
            for c in inspect(engine).get_columns("quant_stock_pool_optional")
        }
        for col in ("dyn_confidence", "stage_batch", "last_confirmed",
                    "radar_theme", "kicked_reason"):
            assert col in cols
        with engine.begin() as conn:
            row = conn.execute(text(
                "SELECT dyn_confidence, stage_batch, radar_theme, kicked_reason, "
                "confidence FROM quant_stock_pool_optional WHERE symbol='OLD001'"
            )).fetchone()
        # 旧行获得退化基准默认值; 原 confidence 不变
        assert float(row[0]) == 0.5
        assert row[1] == "BODY"
        assert row[2] == ""
        assert row[3] == ""
        assert float(row[4]) == 7.5
        engine.dispose()

    def test_init_migration_idempotent_on_live_schema(self, quant_env):
        from sqlalchemy import inspect

        from ai_stock.quant import db

        engine = db.get_engine()
        # quant_env 已跑过 init_quant_db (含迁移); 再次调用应无副作用
        db._migrate_pool_radar_schema(engine)
        db._migrate_pool_radar_schema(engine)
        cols = {
            c["name"]
            for c in inspect(engine).get_columns("quant_stock_pool_optional")
        }
        assert "dyn_confidence" in cols
        assert "kicked_reason" in cols


# ---------------------------------------------------------------------------
# db_ops 动态字段持久化 (clamp / 部分更新 / kicked_reason)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPoolDynamicPersistence:
    def test_upsert_clamps_and_defaults(self, quant_env):
        from ai_stock.quant import db_ops

        db_ops.upsert_optional_stock({
            "symbol": "500001", "name": "N", "confidence": 7.0,
            "dyn_confidence": 1.5,  # 越界 → clamp 到 1.0
        })
        row = db_ops.get_optional_stock("500001")
        assert row["dyn_confidence"] == 1.0
        assert row["stage_batch"] == "BODY"  # 缺省
        assert row["radar_theme"] == ""      # 缺省
        assert row["last_confirmed"] is not None

    def test_update_dynamic_partial(self, quant_env):
        from ai_stock.quant import db_ops

        _upsert("500002", dyn=0.5, stage="BODY", theme="旧主题")
        db_ops.update_optional_dynamic("500002", dyn_confidence=0.66)
        row = db_ops.get_optional_stock("500002")
        assert row["dyn_confidence"] == pytest.approx(0.66, abs=1e-6)
        # 未传入的字段保持不变
        assert row["stage_batch"] == "BODY"
        assert row["radar_theme"] == "旧主题"

    def test_update_status_records_kicked_reason(self, quant_env):
        from ai_stock.quant import db_ops

        _upsert("500003", dyn=0.5)
        db_ops.update_optional_status(
            "500003", "removed", remove_reason="清理官移出", kicked_reason="鱼尾",
        )
        row = db_ops.get_optional_stock("500003")
        assert row["status"] == "removed"
        assert row["remove_reason"] == "清理官移出"
        assert row["kicked_reason"] == "鱼尾"


# ---------------------------------------------------------------------------
# FSM 步骤序列 + handler 注册
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFlowAndRegistration:
    def test_buy_flow_step_sequence(self):
        from ai_stock.quant.orchestrator import FLOW_DEFINITIONS

        steps = [s["step"] for s in FLOW_DEFINITIONS["buy"]["steps"]]
        assert steps == [
            "watchlist_kicker",       # 清理官前置: 硬性过滤 (不经 LLM)
            "logic_collapse_check",
            "tech_signal",
            "catalyst_event",
            "second_verification",
            "position_open",
        ]

    def test_selection_flow_step_sequence(self):
        from ai_stock.quant.orchestrator import FLOW_DEFINITIONS

        steps = [s["step"] for s in FLOW_DEFINITIONS["selection"]["steps"]]
        assert steps == [
            "macro_event", "theme_radar", "limit_up_monitor",
            "industry_scan", "stock_selection", "confidence_maintain",
        ]

    def test_new_handlers_registered(self, quant_env):
        from ai_stock.quant.agents import build_handlers
        from ai_stock.quant.config import BUY_QUEUE, SELECTION_QUEUE

        handlers = build_handlers()
        assert "theme_radar" in handlers[SELECTION_QUEUE]
        assert "confidence_maintain" in handlers[SELECTION_QUEUE]
        assert "watchlist_kicker" in handlers[BUY_QUEUE]
