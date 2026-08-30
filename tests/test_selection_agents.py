"""选股 Agent 集群单元测试 (重构后: 宏观事件 → 生命周期 → 合并深析精选).

覆盖需求:
- MacroEventAgent JSON 解析容错 + 空新闻/LLM 不可用降级 (decision=empty 不阻断)
- 事件-行业知识库: 关键词粗筛 + LLM/KB 交集收敛 (优化点 2)
- IndustryScanAgent LLM 动态加权 + 静态兜底 + 涨停潮阶段修正器 (优化点 3/5)
  + 无事件回退涨幅榜/资金异动量化扫描 (优化点 6 Level 2)
- StockSelectionAgent 合并深析: 综合分硬门槛 + 置信度门槛 + 同分按弹性排序
  + ST/北交所硬过滤 + 直接写自选池 + 无选股时自选池续持降级 (优化点 1/6)
- 核心财务指标预提取 (优化点 4)
- Orchestrator FSM selection 流程步骤顺序 (无独立 deep_analysis 步)

全部走 SQLite 降级队列 + 临时 DB + Mock 数据服务, 不依赖外部行情/真实 LLM.
"""

import pytest


@pytest.fixture()
def quant_env(tmp_path, monkeypatch):
    """独立临时 quant DB + SQLite 队列后端 (与 test_quant_core 同口径)."""
    from ai_stock.quant import db, mq

    db_url = f"sqlite:///{tmp_path / 'quant_selection_test.db'}"
    monkeypatch.setenv("QUANT_DB_URL", db_url)
    monkeypatch.setattr(mq, "MQ_BACKEND", "sqlite")
    db.reset_engine(db_url)
    mq.reset_mq_backend()
    db.init_quant_db()
    # 新闻榜落库是 MacroEventAgent 的副作用, 单测不写真库.
    monkeypatch.setattr(
        "ai_stock.pipeline.news_board.save_news_board",
        lambda news, top_n=None: {"snapshot_id": None, "saved": 0},
    )
    yield db_url
    mq.reset_mq_backend()


class FakeDataService:
    """Mock 数据服务: 仅实现选股集群用到的接口."""

    def __init__(self):
        self.boards = []
        self.industry_stocks: dict[str, list[dict]] = {}
        self.hot_news: list[dict] = []
        self.quotes: dict[str, dict] = {}
        self.fundamentals: dict[str, str] = {}
        self.stock_news: dict[str, list[dict]] = {}

    def get_hot_news(self, days=3):
        return self.hot_news

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
        return self.stock_news.get(symbol, [])

    def get_limit_up_stocks(self, days=1):
        return []


def _make_agent(agent_cls, data: FakeDataService, llm_available: bool = True):
    from ai_stock.quant.llm_helper import QuantLLM

    agent = agent_cls(data_service=data)
    if llm_available:
        agent._llm_holder = QuantLLM(llm_quick=object(), llm_deep=object())
    else:
        agent._llm_holder = QuantLLM(None, None)
    return agent


def _task(context: dict | None = None) -> dict:
    return {
        "task_id": "t1",
        "payload": {
            "flow_id": "selection_test_flow",
            "flow_type": "selection",
            "step": "",
            "context": context or {},
        },
    }


# ---------------------------------------------------------------------------
# FSM 步骤顺序 (优化点 1: 深度分析合并, 无独立 deep_analysis 步)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSelectionFlowDefinition:
    def test_step_order_macro_first_and_wave_before_scan(self):
        from ai_stock.quant.orchestrator import FLOW_DEFINITIONS

        steps = [s["step"] for s in FLOW_DEFINITIONS["selection"]["steps"]]
        assert steps == [
            "macro_event",
            "limit_up_monitor",  # 前置: 涨停潮信号供行业扫描阶段修正引用
            "industry_scan",
            "stock_selection",   # 合并深度分析, 直接入自选池
        ]

    def test_macro_event_handler_registered(self, quant_env):
        from ai_stock.quant.agents import build_handlers
        from ai_stock.quant.config import SELECTION_QUEUE

        handlers = build_handlers()
        assert "macro_event" in handlers[SELECTION_QUEUE]
        assert "deep_analysis" not in handlers[SELECTION_QUEUE]


# ---------------------------------------------------------------------------
# 事件-行业知识库 (优化点 2: 幻觉抑制)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEventIndustryKB:
    def test_match_industries_by_keyword(self):
        from ai_stock.quant.event_industry_kb import match_industries

        assert "半导体" in match_industries("国产算力芯片重大突破")
        assert "电池" in match_industries("固态电池装车量产")
        assert match_industries("") == []
        assert match_industries("毫无关联的日常新闻") == []

    def test_reconcile_intersection_and_fallback(self):
        from ai_stock.quant.event_industry_kb import reconcile

        # 知识库无命中 → 保留 LLM 结果 (兜底不拦截)
        assert reconcile(["石油"], []) == ["石油"]
        # 有交集 → 保留 LLM 侧标签
        assert reconcile(["半导体"], ["半导体", "通信设备"]) == ["半导体"]
        # 交集为空 → 用知识库结果替换 (压制幻觉)
        assert reconcile(["石油行业"], ["农牧饲渔"]) == ["农牧饲渔"]
        assert reconcile([], []) == []


# ---------------------------------------------------------------------------
# MacroEventAgent (知识库预检 + 影响力排序)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMacroEventAgent:
    def test_empty_news_returns_empty_without_llm(self, quant_env):
        from ai_stock.quant.agents.macro_event import MacroEventAgent

        data = FakeDataService()
        data.hot_news = []
        agent = _make_agent(MacroEventAgent, data, llm_available=False)
        result = agent.handle(_task())
        assert result["decision"] == "empty"
        assert result["events"] == []

    def test_llm_unavailable_degrades_to_empty(self, quant_env):
        from ai_stock.quant.agents.macro_event import MacroEventAgent

        data = FakeDataService()
        data.hot_news = [{"title": "某重磅政策发布", "content": "内容", "time": "x"}]
        agent = _make_agent(MacroEventAgent, data, llm_available=False)
        result = agent.handle(_task())
        assert result["decision"] == "empty"
        assert result["news_scanned"] == 1

    def test_events_sorted_by_influence_desc(self, quant_env, monkeypatch):
        from ai_stock.quant.agents import macro_event
        from ai_stock.quant.agents.macro_event import (
            MacroEvent,
            MacroEventAgent,
            MacroEventReport,
        )

        data = FakeDataService()
        data.hot_news = [
            {"title": f"新闻{i}", "content": "摘要", "source": "财联社", "time": "t"}
            for i in range(3)
        ]
        # 乱序返回, 验证 Agent 侧按影响力降序重排 + 剔除无行业映射事件
        monkeypatch.setattr(
            macro_event, "structured_invoke",
            lambda llm, schema, prompt, **kw: MacroEventReport(events=[
                MacroEvent(title="行业级事件", level="industry",
                           impact_industries=["光伏"], influence_score=6.0),
                MacroEvent(title="全球变革事件", level="global",
                           impact_industries=["半导体", "通信"], influence_score=9.5),
                MacroEvent(title="无行业映射", level="national",
                           impact_industries=[], influence_score=10.0),
                MacroEvent(title="国家级战略", level="national",
                           impact_industries=["新能源", "电力"], influence_score=8.0),
            ]),
        )
        agent = _make_agent(MacroEventAgent, data)
        result = agent.handle(_task())
        assert result["decision"] == "ok"
        titles = [e["title"] for e in result["events"]]
        assert titles == ["全球变革事件", "国家级战略", "行业级事件"]
        assert result["events"][0]["impact_industries"] == ["半导体", "通信"]

    def test_malformed_llm_output_is_tolerated(self, quant_env, monkeypatch):
        """JSON 解析/调用异常 → decision=empty, 不抛出阻断 Orchestrator."""
        from ai_stock.quant.agents import macro_event
        from ai_stock.quant.agents.macro_event import MacroEventAgent

        data = FakeDataService()
        data.hot_news = [{"title": "新闻", "content": "摘要"}]

        def _raise(llm, schema, prompt, **kw):
            raise RuntimeError("LLM 结构化输出解析失败")

        monkeypatch.setattr(macro_event, "structured_invoke", _raise)
        agent = _make_agent(MacroEventAgent, data)
        result = agent.handle(_task())
        assert result["decision"] == "empty"
        assert result["events"] == []


# ---------------------------------------------------------------------------
# IndustryScanAgent (优化点 3: 动态加权 + 优化点 5: 涨停潮阶段修正器)
# ---------------------------------------------------------------------------

_SCAN_BOARDS = [
    {"code": "BK001", "name": "半导体", "change_pct": 2.0, "main_net_inflow": 3e8,
     "up_count": 40, "down_count": 10, "top_stock_name": "甲股",
     "top_stock_code": "600001", "board_level": "industry"},
    {"code": "BK002", "name": "房地产", "change_pct": -1.0, "main_net_inflow": -2e8,
     "up_count": 5, "down_count": 60, "top_stock_name": "",
     "top_stock_code": "", "board_level": "industry"},
    {"code": "BK003", "name": "电力", "change_pct": 3.0, "main_net_inflow": 1e8,
     "up_count": 30, "down_count": 8, "top_stock_name": "乙股",
     "top_stock_code": "600002", "board_level": "industry"},
    {"code": "BK004", "name": "光伏设备", "change_pct": 1.5, "main_net_inflow": 2e8,
     "up_count": 20, "down_count": 6, "top_stock_name": "丙股",
     "top_stock_code": "600003", "board_level": "industry"},
    {"code": "BK005", "name": "白酒", "change_pct": 0.3, "main_net_inflow": 0.0,
     "up_count": 10, "down_count": 10, "top_stock_name": "",
     "top_stock_code": "", "board_level": "industry"},
]


@pytest.mark.unit
class TestIndustryScanAgent:
    def test_dynamic_priority_with_wave_stage_corrector(self, quant_env, monkeypatch):
        """LLM 动态 priority 排序; 涨停潮将导入期强制升爆发前期 (优化点 3/5)."""
        from ai_stock.quant.agents import selection
        from ai_stock.quant.agents.selection import (
            IndustryLifecycleList,
            IndustryLifecycleReport,
            IndustryScanAgent,
        )

        data = FakeDataService()
        data.boards = _SCAN_BOARDS
        context = {
            "macro_event": {"events": [
                {"title": "AI 算力政策", "level": "national",
                 "impact_industries": ["半导体"], "influence_score": 9.0},
                {"title": "碳中和推进", "level": "national",
                 "impact_industries": ["新能源", "电力"], "influence_score": 8.0},
            ]},
            "limit_up_monitor": {"waves": [{"name": "半导体", "limit_up_count": 5}]},
        }
        monkeypatch.setattr(
            selection, "structured_invoke",
            lambda llm, schema, prompt, **kw: IndustryLifecycleList(industries=[
                # 导入期 + 涨停潮 → 强制升爆发前期, priority 抬到下限 8.0 再 +1
                IndustryLifecycleReport(name="半导体", stage="导入期",
                                        analysis="政策催化", priority=2.0),
                IndustryLifecycleReport(name="光伏设备", stage="爆发前期",
                                        analysis="需求启动", priority=7.0),
                IndustryLifecycleReport(name="电力", stage="爆发期",
                                        analysis="供不应求", priority=8.5),
                # priority=0 → 回退静态兜底表 (成熟期 0 / 衰退期 -1)
                IndustryLifecycleReport(name="白酒", stage="成熟期",
                                        analysis="增速放缓", priority=0.0),
                IndustryLifecycleReport(name="房地产", stage="衰退期",
                                        analysis="需求萎缩", priority=0.0),
            ]),
        )
        agent = _make_agent(IndustryScanAgent, data)
        result = agent.handle(_task(context))

        assert result["decision"] == "ok"
        top = result["top_industries"]
        assert len(top) == 4  # MAX_INDUSTRIES_OUTPUT
        assert [i["name"] for i in top] == ["半导体", "电力", "光伏设备", "白酒"]
        # 涨停潮修正器: 导入期 → 爆发前期, max(2.0, 8.0) + 1.0 = 9.0
        assert top[0]["stage"] == "爆发前期"
        assert top[0]["stage_corrected"] is True
        assert top[0]["priority"] == 9.0
        assert top[0]["event_tag"] == "半导体"
        assert top[1]["priority"] == 8.5
        assert top[3]["priority"] == 0.0   # 白酒: LLM 未加权 → 静态兜底
        assert result["industries"] == result["top_industries"]
        assert result["stage_distribution"]["爆发前期"] == 2

    def test_unknown_stage_normalized_to_import(self, quant_env, monkeypatch):
        """LLM 输出非八档阶段值时归一为 导入期, 不无声丢弃."""
        from ai_stock.quant.agents import selection
        from ai_stock.quant.agents.selection import (
            IndustryLifecycleList,
            IndustryLifecycleReport,
            IndustryScanAgent,
        )

        data = FakeDataService()
        data.boards = _SCAN_BOARDS
        monkeypatch.setattr(
            selection, "structured_invoke",
            lambda llm, schema, prompt, **kw: IndustryLifecycleList(industries=[
                IndustryLifecycleReport(name="电力", stage="不存在的阶段", analysis="x"),
            ]),
        )
        agent = _make_agent(IndustryScanAgent, data)
        result = agent.handle(_task())  # 无事件 → 回退涨幅榜
        assert result["decision"] == "ok"
        assert all(i["stage"] == "导入期" for i in result["top_industries"])

    def test_fallback_to_top_gainers_without_events(self, quant_env, monkeypatch):
        """优化点 6 Level 2: 无事件时涨幅榜/资金异动量化扫描补足候选."""
        from ai_stock.quant.agents import selection
        from ai_stock.quant.agents.selection import (
            IndustryLifecycleList,
            IndustryLifecycleReport,
            IndustryScanAgent,
        )

        data = FakeDataService()
        data.boards = _SCAN_BOARDS
        monkeypatch.setattr(
            selection, "structured_invoke",
            lambda llm, schema, prompt, **kw: IndustryLifecycleList(industries=[
                IndustryLifecycleReport(name="电力", stage="爆发期", analysis="x"),
                IndustryLifecycleReport(name="光伏设备", stage="导入期", analysis="x"),
                IndustryLifecycleReport(name="白酒", stage="成熟期", analysis="x"),
                IndustryLifecycleReport(name="房地产", stage="衰退期", analysis="x"),
            ]),
        )
        agent = _make_agent(IndustryScanAgent, data)
        result = agent.handle(_task())  # 空 context → 回退涨幅前 10 行业
        assert result["decision"] == "ok"
        # priority 全为 0 → 静态兜底: 爆发期(3) > 导入期(2) > 成熟期(0) > 衰退期(-1)
        assert result["top_industries"][0]["name"] == "电力"
        assert result["top_industries"][0]["priority"] == 3.0

    def test_llm_unavailable_returns_empty(self, quant_env):
        from ai_stock.quant.agents.selection import IndustryScanAgent

        data = FakeDataService()
        data.boards = _SCAN_BOARDS
        agent = _make_agent(IndustryScanAgent, data, llm_available=False)
        result = agent.handle(_task())
        assert result["decision"] == "empty"
        assert result["top_industries"] == []

    def test_no_boards_returns_empty(self, quant_env):
        from ai_stock.quant.agents.selection import IndustryScanAgent

        agent = _make_agent(IndustryScanAgent, FakeDataService())
        result = agent.handle(_task())
        assert result["decision"] == "empty"


# ---------------------------------------------------------------------------
# StockSelectionAgent (优化点 1: 合并深析 + 双门槛入池 + 优化点 6 降级)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStockSelectionAgent:
    def _setup_data(self) -> FakeDataService:
        data = FakeDataService()
        data.industry_stocks["BK001"] = [
            {"code": "600519", "name": "龙头股", "turnover_rate": 3.0,
             "volume_ratio": 1.5, "market_cap": 2e11, "amount": 5e9},
            {"code": "688001", "name": "弹性股", "turnover_rate": 12.0,
             "volume_ratio": 2.5, "market_cap": 2e10, "amount": 3e9},
            {"code": "000001", "name": "弱势股", "turnover_rate": 1.0,
             "volume_ratio": 0.8, "market_cap": 1e10, "amount": 2e9},
            # ST / 北交所: 硬过滤全程排除
            {"code": "830001", "name": "*ST退市", "turnover_rate": 5.0,
             "volume_ratio": 1.0, "market_cap": 1e9, "amount": 1e9},
            {"code": "430002", "name": "北交所股", "turnover_rate": 5.0,
             "volume_ratio": 1.0, "market_cap": 1e9, "amount": 1e9},
        ]
        for code in ("600519", "688001", "000001"):
            data.quotes[code] = {
                "name": code, "change_pct": 1.0, "market_cap": 1e10,
            }
            data.fundamentals[code] = (
                f"Name: {code}\nPE (TTM): 25.3\nPB: 3.1\nROE (%): 18.5\n"
                "PEG: 1.2\nForward PE (FY2026): 20.5x (price=10, EPS=0.5)\n"
                "Market Cap (100M CNY): 2000.0"
            )
        return data

    def test_merged_deep_eval_dual_threshold_and_pool_write(
        self, quant_env, monkeypatch,
    ):
        """合并深析: 综合分门槛 + 置信度门槛 + 同分按弹性 + 直接写自选池."""
        from ai_stock.quant import db_ops
        from ai_stock.quant.agents import selection
        from ai_stock.quant.agents.selection import (
            StockDeepEval,
            StockDeepEvalList,
            StockSelectionAgent,
        )

        data = self._setup_data()
        context = {"industry_scan": {"top_industries": [
            {"code": "BK001", "name": "半导体", "stage": "验证后期",
             "event_tag": "半导体"},
        ]}}
        monkeypatch.setattr(
            selection, "structured_invoke",
            lambda llm, schema, prompt, **kw: StockDeepEvalList(stocks=[
                StockDeepEval(code="600519", name="龙头股", score=8.0,
                              elasticity_score=6.0, reason="地位稳固, 财务健康",
                              bull_factors=["市占率第一"], bear_factors=["估值偏高"],
                              stage_judgement="行业爆发前夕",
                              rise_trigger="业绩兑现", confidence=8.5,
                              risk_tags=["估值偏高"]),
                StockDeepEval(code="688001", name="弹性股", score=8.0,
                              elasticity_score=9.0, reason="壁垒深, 成长快",
                              confidence=5.5),  # 低于置信度门槛 6.0 → 不入池
                # 低于综合分硬门槛 5.0 → 剔除并计数
                StockDeepEval(code="000001", name="弱势股", score=4.0,
                              confidence=7.0, reason="基本面恶化"),
                # LLM 虚构代码 → 丢弃
                StockDeepEval(code="999999", name="幽灵股", score=9.0,
                              confidence=9.0, reason="虚构"),
            ]),
        )
        agent = _make_agent(StockSelectionAgent, data)
        result = agent.handle(_task(context))

        assert result["decision"] == "ok"
        assert result["evaluated"] == 3          # ST/北交所已被硬过滤
        assert result["dropped_low_score"] == 1  # 综合评分不足剔除计数
        assert result["skipped_low_confidence"] == 1
        assert result["pool_written"] == 1
        symbols = [s["symbol"] for s in result["selected"]]
        # 同分 8.0 → 弹性分高者优先
        assert symbols == ["688001", "600519"]
        # Context 瘦身 (优化点 7): 上报只保留关键结论字段
        first = result["selected"][0]
        assert set(first.keys()) == {"symbol", "name", "industry", "score",
                                     "confidence"}
        assert first["industry"] == "半导体"
        # 双门槛落库: 600519 入池, 688001 置信度不足不入池
        row = db_ops.get_optional_stock("600519")
        assert row is not None
        assert row["confidence"] == 8.5
        assert row["report"].startswith("入选理由:")
        assert row["bull_factors"] == ["市占率第一"]
        assert db_ops.get_optional_stock("688001") is None

    def test_no_picks_falls_back_to_optional_pool(self, quant_env, monkeypatch):
        """优化点 6 Level 3: 无合格选股 → 昨日自选池续持推荐 (不重复写库)."""
        from ai_stock.quant import db_ops
        from ai_stock.quant.agents import selection
        from ai_stock.quant.agents.selection import (
            StockDeepEvalList,
            StockSelectionAgent,
        )

        # 预置自选池 (模拟昨日入池标的)
        db_ops.upsert_optional_stock({
            "symbol": "300750", "name": "存量池股", "industry": "电池",
            "reason": "昨日入选", "bull_factors": ["产能扩张"],
            "bear_factors": [], "stage_judgement": "行业爆发前夕",
            "rise_trigger": "订单落地", "risk_tags": [], "confidence": 7.5,
            "report": "入选理由: 昨日入选",
        })
        data = self._setup_data()
        context = {"industry_scan": {"top_industries": [
            {"code": "BK001", "name": "半导体", "stage": "爆发期"},
        ]}}
        monkeypatch.setattr(
            selection, "structured_invoke",
            lambda llm, schema, prompt, **kw: StockDeepEvalList(stocks=[]),
        )
        agent = _make_agent(StockSelectionAgent, data)
        result = agent.handle(_task(context))

        assert result["decision"] == "fallback"
        assert result["pool_written"] == 0       # 续持不重复写库
        assert [s["symbol"] for s in result["selected"]] == ["300750"]

    def test_llm_unavailable_returns_empty(self, quant_env):
        from ai_stock.quant.agents.selection import StockSelectionAgent

        data = self._setup_data()
        context = {"industry_scan": {"top_industries": [
            {"code": "BK001", "name": "半导体", "stage": "爆发期"},
        ]}}
        agent = _make_agent(StockSelectionAgent, data, llm_available=False)
        result = agent.handle(_task(context))
        # 自选池为空 → Level 3 也无标的 → empty
        assert result["decision"] == "empty"
        assert result["selected"] == []

    def test_no_top_industries_returns_empty(self, quant_env):
        from ai_stock.quant.agents.selection import StockSelectionAgent

        agent = _make_agent(StockSelectionAgent, self._setup_data())
        result = agent.handle(_task({"industry_scan": {"top_industries": []}}))
        assert result["decision"] == "empty"


# ---------------------------------------------------------------------------
# 核心财务指标预提取 (优化点 4)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFundMetricsExtraction:
    def test_extract_metrics_from_fundamentals_text(self):
        from ai_stock.quant.agents.selection import StockSelectionAgent

        text = (
            "Name: 600519\nPE (TTM): 25.3\nPB: 3.1\nROE (%): 18.5\n"
            "PEG: 1.2\nForward PE (FY2026): 20.5x (price=10, EPS=0.5)\n"
            "Market Cap (100M CNY): 2000.0"
        )
        metrics = StockSelectionAgent._extract_fund_metrics(text)
        assert "PE(TTM)=25.3" in metrics
        assert "PB=3.1" in metrics
        assert "ROE%=18.5" in metrics
        assert "PEG=1.2" in metrics
        assert "FwdPE(FY2026)=20.5" in metrics
        assert "市值亿=2000.0" in metrics

    def test_extract_metrics_empty_on_bad_input(self):
        from ai_stock.quant.agents.selection import StockSelectionAgent

        assert StockSelectionAgent._extract_fund_metrics("") == ""
        assert StockSelectionAgent._extract_fund_metrics("无指标文本") == ""


# ---------------------------------------------------------------------------
# 数据层接口 (阶段一)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDataServiceInterfaces:
    def test_get_industry_stocks_truncates_and_detail_lookup(
        self, quant_env, monkeypatch,
    ):
        from ai_stock.quant import data_service

        stocks = [{"code": f"60{i:04d}", "name": f"股{i}"} for i in range(30)]
        monkeypatch.setattr(
            "ai_stock.dataflows.pipeline_data.get_board_constituents",
            lambda board_code, top_n=20, sort_by="amount": stocks,
        )
        svc = data_service.DataService()
        out = svc.get_industry_stocks("BK001", top_n=20)
        assert len(out) == 20
        assert svc.get_industry_stocks("", top_n=20) == []
        assert svc.get_industry_detail("") is None
