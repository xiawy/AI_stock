"""选股 Agent 集群单元测试 (重构后: 宏观事件 → 生命周期 → 合并深析精选).

覆盖需求:
- MacroEventAgent JSON 解析容错 + 空新闻/LLM 不可用降级 (decision=empty 不阻断)
- 事件类型分类 (first_proposal/政策支持等) 透传与生命周期 Prompt 新主题提示,
  仅作提示不参与加权评分 (轻量级增强: "首次提出→业绩验证"闭环)
- 事件-行业知识库: 关键词粗筛 + LLM/KB 交集收敛 (优化点 2)
- IndustryScanAgent 供需×生命周期加权综合排名 (供需 40% + 阶段 30% +
  上游传导 15% + 涨停潮 15%) + 供需强势阶段上调 + 上游传导二次验证 (幻觉过滤)
  + 无事件回退涨幅榜/资金异动量化扫描 (优化点 3/5/6)
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
                           impact_industries=["半导体", "通信"], influence_score=9.5,
                           event_type="first_proposal"),
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
        # event_type 分类透传 (首次提出标记); 未输出时默认常规事件 (向后兼容)
        assert result["events"][0]["event_type"] == "first_proposal"
        assert result["events"][2]["event_type"] == "routine"

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
# IndustryScanAgent (供需第一性原理 × 生命周期矩阵 × 上游传导 加权综合排名)
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
    def test_weighted_composite_ranking(self, quant_env, monkeypatch):
        """供需 40% + 阶段 30% + 上游传导 15% + 涨停潮 15% 加权综合排名;
        上游传导加分/涨停潮加分/供需强势阶段上调均生效."""
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
            "limit_up_monitor": {"waves": [{"name": "电力", "limit_up_count": 5}]},
        }
        monkeypatch.setattr(
            selection, "structured_invoke",
            lambda llm, schema, prompt, **kw: IndustryLifecycleList(industries=[
                # 供需 8.0×0.4 + 验证后期 5/5×0.3 + 长期 0 + 极高 +0.025 = 6.45;
                # 点名上游"电力" → 电力获传导加分 (自身不受影响)
                IndustryLifecycleReport(
                    name="半导体", stage="验证后期", analysis="AI 算力需求爆发",
                    imbalance_score=8.0, expansion_cycle="长期",
                    entry_barrier="极高", transmission_upstream=["电力"],
                    transmission_evidence="晶圆厂用电需求激增",
                ),
                # 供需 6.0×0.4 + 爆发前期 4/5×0.3 + 上游 1.5 + 涨停潮 1.5 +
                # 中期 -0.025 + 高 0 = 5.00 (双加分生效)
                IndustryLifecycleReport(
                    name="电力", stage="爆发前期", analysis="用电缺口",
                    imbalance_score=6.0, expansion_cycle="中期",
                    entry_barrier="高",
                ),
                # 成熟期阶段分 0 但供需失衡 7.5 ≥ 6 → 强制上调爆发前期,
                # 7.5×0.4 + 4/5×0.3 + 短期 -0.05 + 低 -0.05 = 4.40 (供需强势修正)
                IndustryLifecycleReport(
                    name="白酒", stage="成熟期", analysis="供需意外紧张",
                    imbalance_score=7.5, expansion_cycle="短期",
                    entry_barrier="低",
                ),
                # 供需 7.0×0.4 + 导入期 2/5×0.3 - 0.025 - 0.025 = 3.50 (无任何加分)
                IndustryLifecycleReport(
                    name="光伏设备", stage="导入期", analysis="需求启动",
                    imbalance_score=7.0, expansion_cycle="中期",
                    entry_barrier="中",
                ),
                # 衰退期负分 + 无失衡 → 综合分钉在 0 (低于光伏设备, 落选 Top 4)
                IndustryLifecycleReport(
                    name="房地产", stage="衰退期", analysis="需求萎缩",
                ),
            ]),
        )
        agent = _make_agent(IndustryScanAgent, data)
        result = agent.handle(_task(context))

        assert result["decision"] == "ok"
        top = result["top_industries"]
        assert len(top) == 4  # MAX_INDUSTRIES_OUTPUT
        assert [i["name"] for i in top] == ["半导体", "电力", "白酒", "光伏设备"]
        # 供需维度字段透传至 slim 输出
        assert top[0]["priority"] == 6.45
        assert top[0]["imbalance_score"] == 8.0
        assert top[0]["expansion_cycle"] == "长期"
        assert top[0]["entry_barrier"] == "极高"
        assert top[0]["transmission_upstream"] == ["电力"]
        assert top[0]["stage"] == "验证后期"
        assert top[0]["event_tag"] == "半导体"
        # 上游传导加分 (被半导体点名) + 涨停潮加分 (右侧确认)
        assert top[1]["upstream_bonus"] == 1.5
        assert top[1]["wave_bonus"] == 1.5
        assert top[1]["priority"] == 5.0
        # 供需强势修正: 成熟期(阶段分 0) + 失衡 7.5 ≥ 6 → 爆发前期, stage_corrected 置位
        assert top[2]["stage"] == "爆发前期"
        assert top[2]["stage_corrected"] is True
        assert top[2]["priority"] == 4.4
        assert top[3]["upstream_bonus"] == 0
        assert top[3]["wave_bonus"] == 0
        assert top[3]["priority"] == 3.5
        assert result["industries"] == result["top_industries"]
        assert result["stage_distribution"]["爆发前期"] == 2

    def test_transmission_master_match_and_hallucination_filter(
        self, quant_env, monkeypatch,
    ):
        """上游传导二次验证: 未上榜上游匹配板块库 → 衰减建档附加展示在行业榜;
        虚构上游丢弃 (幻觉抑制); 已上榜上游不重复; 条数上限生效."""
        from ai_stock.quant.agents import selection
        from ai_stock.quant.agents.selection import (
            IndustryLifecycleList,
            IndustryLifecycleReport,
            IndustryScanAgent,
        )

        data = FakeDataService()
        # 覆铜板/环氧树脂/玻纤布涨幅不足 → 落选候选池, 用于验证板块库建档路径
        data.boards = _SCAN_BOARDS + [
            {"code": "BK006", "name": "覆铜板", "change_pct": 0.1,
             "main_net_inflow": 1e8, "up_count": 8, "down_count": 4,
             "top_stock_name": "丁股", "top_stock_code": "600004",
             "board_level": "industry"},
            {"code": "BK007", "name": "银行", "change_pct": 0.2,
             "main_net_inflow": 0.0, "up_count": 6, "down_count": 6,
             "top_stock_name": "", "top_stock_code": "",
             "board_level": "industry"},
            {"code": "BK008", "name": "环氧树脂", "change_pct": 0.05,
             "main_net_inflow": 0.0, "up_count": 3, "down_count": 3,
             "top_stock_name": "", "top_stock_code": "",
             "board_level": "industry"},
            {"code": "BK009", "name": "玻纤布", "change_pct": 0.02,
             "main_net_inflow": 0.0, "up_count": 2, "down_count": 2,
             "top_stock_name": "", "top_stock_code": "",
             "board_level": "industry"},
        ]
        monkeypatch.setattr(selection, "MAX_SCAN_CANDIDATES", 5)
        context = {
            "macro_event": {"events": [
                {"title": "AI 算力政策", "level": "national",
                 "impact_industries": ["半导体"], "influence_score": 9.0},
                {"title": "碳中和推进", "level": "national",
                 "impact_industries": ["新能源", "电力"], "influence_score": 8.0},
            ]},
        }
        monkeypatch.setattr(
            selection, "structured_invoke",
            lambda llm, schema, prompt, **kw: IndustryLifecycleList(industries=[
                IndustryLifecycleReport(
                    name="半导体", stage="验证后期", analysis="AI 需求",
                    imbalance_score=8.0, expansion_cycle="长期",
                    entry_barrier="极高",
                    # 电力已上榜 → 跳过; 量子材料 → 幻觉丢弃; 其余三个建档 (达上限)
                    transmission_upstream=[
                        "覆铜板", "量子材料", "电力", "环氧树脂", "玻纤布",
                    ],
                    transmission_evidence="XX 覆铜板厂商发布涨价函",
                ),
                IndustryLifecycleReport(name="电力", stage="爆发前期",
                                        analysis="用电缺口", imbalance_score=6.0),
                IndustryLifecycleReport(name="光伏设备", stage="导入期",
                                        analysis="需求启动", imbalance_score=5.0),
                IndustryLifecycleReport(name="白酒", stage="成熟期",
                                        analysis="增速放缓", imbalance_score=1.0),
            ]),
        )
        agent = _make_agent(IndustryScanAgent, data)
        result = agent.handle(_task(context))

        assert result["decision"] == "ok"
        # Top 不受传导行影响 (二次验证仅行业榜附加展示)
        assert [i["name"] for i in result["top_industries"]] == [
            "半导体", "电力", "光伏设备", "银行",
        ]
        board_names = [i["name"] for i in result["board_industries"]]
        # 传导行按综合分并入行业榜 (覆铜板等 2.94 > 光伏设备 2.7), 恰好 3 条达上限
        assert board_names == [
            "半导体", "电力", "覆铜板", "环氧树脂", "玻纤布",
            "光伏设备", "银行", "白酒",
        ]
        rows = {i["name"]: i for i in result["board_industries"]}
        assert rows["覆铜板"]["transmission_from"] == "半导体"
        assert rows["覆铜板"]["code"] == "BK006"
        assert rows["覆铜板"]["stage"] == "导入期"
        # 失衡分按 TRANSMISSION_DECAY 衰减: 8.0×0.7 = 5.6, 保守计分 2.94
        assert rows["覆铜板"]["imbalance_score"] == 5.6
        assert rows["覆铜板"]["priority"] == 2.94
        assert "涨价函" in rows["覆铜板"]["stage_analysis"]
        # 虚构上游"量子材料"无法匹配板块库 → 丢弃 (幻觉抑制)
        assert "量子材料" not in board_names
        # 电力已上排名榜 → 不重复附加; 非传导行标记为空 (兼容存量展示)
        assert board_names.count("电力") == 1
        assert rows["半导体"]["transmission_from"] == ""

    def test_transmission_promotes_offboard_candidate(self, quant_env, monkeypatch):
        """上游传导二次验证: 上游为已评估但排在榜外的候选 → 擢升展示并保留完整评估;
        已上榜上游不重复."""
        from ai_stock.quant.agents import selection
        from ai_stock.quant.agents.selection import (
            IndustryLifecycleList,
            IndustryLifecycleReport,
            IndustryScanAgent,
        )

        data = FakeDataService()
        data.boards = _SCAN_BOARDS
        monkeypatch.setattr(selection, "INDUSTRY_BOARD_SIZE", 2)
        context = {"macro_event": {"events": [
            {"title": "AI 算力政策", "level": "national",
             "impact_industries": ["半导体"], "influence_score": 9.0},
        ]}}
        monkeypatch.setattr(
            selection, "structured_invoke",
            lambda llm, schema, prompt, **kw: IndustryLifecycleList(industries=[
                IndustryLifecycleReport(
                    name="半导体", stage="验证后期", analysis="AI 需求",
                    imbalance_score=8.0, expansion_cycle="长期",
                    entry_barrier="极高",
                    # 白酒为候选但排在榜外 → 擢升; 电力已上榜 → 不重复 (测试用例仅为验证机制)
                    transmission_upstream=["白酒", "电力"],
                    transmission_evidence="上游材料瓶颈",
                ),
                IndustryLifecycleReport(name="电力", stage="爆发前期",
                                        analysis="用电缺口", imbalance_score=3.0),
                IndustryLifecycleReport(name="光伏设备", stage="导入期",
                                        analysis="需求启动", imbalance_score=2.0),
                IndustryLifecycleReport(name="白酒", stage="成熟期",
                                        analysis="增速放缓", imbalance_score=0.5),
            ]),
        )
        agent = _make_agent(IndustryScanAgent, data)
        result = agent.handle(_task(context))

        assert result["decision"] == "ok"
        board_names = [i["name"] for i in result["board_industries"]]
        # 排名榜前 2 = 半导体/电力; 白酒排在榜外但作为上游被擢升展示 (附加在尾部)
        assert board_names == ["半导体", "电力", "白酒"]
        rows = {i["name"]: i for i in result["board_industries"]}
        assert rows["白酒"]["transmission_from"] == "半导体"
        # 擢升复用完整评估: 成熟期阶段分 0, 0.5×0.4 + 传导加分 0.0225 - 0.05 → 钉在 0.0
        assert rows["白酒"]["stage"] == "成熟期"
        assert rows["白酒"]["priority"] == 0.0
        assert board_names.count("电力") == 1
        # Top 不受擢升影响 (白酒综合分垫底, 房地产静态兜底 0.7 反超)
        assert [i["name"] for i in result["top_industries"]] == [
            "半导体", "电力", "光伏设备", "房地产",
        ]

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
        # 无供需分项 → 纯阶段分 + 周期/壁垒默认微调:
        # 爆发期 3/5×0.3 - 0.025 - 0.025 = 1.30, 仍居首 (阶段排序逻辑保留)
        assert result["top_industries"][0]["name"] == "电力"
        assert result["top_industries"][0]["priority"] == 1.3

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

    def test_first_proposal_hint_in_lifecycle_prompt(self, quant_env, monkeypatch):
        """轻量级增强: first_proposal 事件透传新主题提示 + 业绩验证降级规则 +
        机器人产业链细分示例进入生命周期 Prompt; 仅作提示, 不改变加权公式."""
        from ai_stock.quant.agents import selection
        from ai_stock.quant.agents.selection import (
            IndustryLifecycleList,
            IndustryLifecycleReport,
            IndustryScanAgent,
        )

        data = FakeDataService()
        data.boards = _SCAN_BOARDS + [
            {"code": "BK006", "name": "机器人", "change_pct": 5.0,
             "main_net_inflow": 4e8, "up_count": 30, "down_count": 2,
             "top_stock_name": "某龙头", "top_stock_code": "600006",
             "board_level": "industry"},
        ]
        context = {"macro_event": {"events": [
            {"title": "人形机器人概念首次提出", "level": "national",
             "event_type": "first_proposal",
             "impact_industries": ["机器人"], "influence_score": 9.5},
        ]}, "trade_date": "2026-08-28"}
        captured: dict = {}

        def _capture(llm, schema, prompt, **kw):
            captured["prompt"] = prompt
            return IndustryLifecycleList(industries=[
                IndustryLifecycleReport(
                    name="机器人", stage="爆发前期",
                    analysis="全新题材, 预期 12-18 个月兑现",
                    imbalance_score=7.0, expansion_cycle="长期",
                    entry_barrier="高",
                ),
            ])

        monkeypatch.setattr(selection, "structured_invoke", _capture)
        agent = _make_agent(IndustryScanAgent, data)
        result = agent.handle(_task(context))
        assert result["decision"] == "ok"

        prompt = captured["prompt"]
        # 1) 当前评估日期明示 (时间感知: 供 LLM 判断距题材首提的跨度)
        assert "当前评估日期: 2026-08-28" in prompt
        # 2) 事件行与候选行均标注"首次提出"新主题标记 (供 LLM 识别)
        assert "人形机器人概念首次提出 (首次提出)" in prompt
        assert "关联事件标签: 机器人 (事件类型: 首次提出)" in prompt
        # 3) 新主题特别规则: 想象力高阶段 + 要求声明兑现窗口 +
        #    结合评估日期跨过业绩披露期无增长则主动降级 (25 年后淡出的规则载体)
        assert "预期兑现的时间窗口" in prompt
        assert "业绩披露期" in prompt
        # 4) 产业链细分挖掘示例 (电机/丝杠/减速器) 引导传导发散
        assert "伺服电机" in prompt
        assert "滚珠丝杠" in prompt
        assert "谐波减速器" in prompt
        # 事件类型仅作提示不参与评分: 机器人综合分仍按原公式计算,
        # 7.0×0.4 + 4/5×0.3 + 长期 0 + 高 0 = 5.20 (无额外加分)
        robot = next(
            i for i in result["top_industries"] if i["name"] == "机器人"
        )
        assert robot["priority"] == 5.2


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
            "Market Cap (100M CNY): 2000.0\n"
            "Financial Report Period (REPORT_DATE): 2025-06-30"
        )
        metrics = StockSelectionAgent._extract_fund_metrics(text)
        # 报告期锚点放首位: 供 LLM 判断财务数据新鲜度 (滞后基本面防线)
        assert metrics.startswith("报告期=2025-06-30")
        assert "PE(TTM)=25.3" in metrics
        assert "PB=3.1" in metrics
        assert "ROE%=18.5" in metrics
        assert "PEG=1.2" in metrics
        assert "FwdPE(FY2026)=20.5" in metrics
        assert "市值亿=2000.0" in metrics

    def test_extract_metrics_without_report_period_still_works(self):
        """旧格式基本面文本 (无报告期行) 提取不受影响 (向后兼容)."""
        from ai_stock.quant.agents.selection import StockSelectionAgent

        text = "Name: 600519\nPE (TTM): 25.3\nPB: 3.1"
        metrics = StockSelectionAgent._extract_fund_metrics(text)
        assert metrics == "PE(TTM)=25.3, PB=3.1"

    def test_latest_report_date_from_em_f10(self, monkeypatch):
        """报告期锚点: 东财 F10 利润表倒序首行 REPORT_DATE; 无数据返空串."""
        from ai_stock.dataflows import a_stock

        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        monkeypatch.setattr(
            a_stock, "_em_get",
            lambda url, params=None, timeout=15, **kw: _Resp(
                {"result": {"data": [
                    {"REPORT_DATE": "2025-06-30T00:00:00"},
                ]}},
            ),
        )
        assert a_stock._em_latest_report_date("600519") == "2025-06-30"

        monkeypatch.setattr(
            a_stock, "_em_get",
            lambda url, params=None, timeout=15, **kw: _Resp({"result": None}),
        )
        assert a_stock._em_latest_report_date("600519") == ""

    def test_extract_metrics_empty_on_bad_input(self):
        from ai_stock.quant.agents.selection import StockSelectionAgent

        assert StockSelectionAgent._extract_fund_metrics("") == ""
        # 无指标文本 → 兜底原文首行摘要 (格式漂移时不丢基本面上下文)
        assert StockSelectionAgent._extract_fund_metrics("无指标文本") == "无指标文本"
        # 兜底跳过标题行/分隔行, 取首行实质内容并截断 80 字符
        fallback = StockSelectionAgent._extract_fund_metrics(
            "# Company Fundamentals for 600519 (A-stock)\n"
            "--- Consensus EPS Forecast ---\n" + "长" * 100,
        )
        assert fallback == "长" * 80
        assert StockSelectionAgent._extract_fund_metrics("# 仅标题\n---\n") == ""

    def test_extract_report_period_label_variants(self):
        """报告期锚点多写法容错: 英文标签/裸字段/中文标签 + 中英文冒号."""
        from ai_stock.quant.agents.selection import StockSelectionAgent

        for text in (
            "Financial Report Period (REPORT_DATE): 2025-06-30",
            "REPORT_DATE: 2025-03-31",
            "报告期：2024-12-31",
        ):
            metrics = StockSelectionAgent._extract_fund_metrics(text)
            assert metrics.startswith("报告期="), text


# ---------------------------------------------------------------------------
# 行业模糊匹配 (后缀剥离核心词, 替代字符集 rstrip)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMatchTag:
    def test_bidirectional_containment(self):
        from ai_stock.quant.agents.selection import IndustryScanAgent

        match = IndustryScanAgent._match_tag
        assert match("半导体", "半导体") is True           # 全同包含 (原行为保留)
        assert match("人形机器人", "机器人") is True      # tag 被板名包含 (原行为保留)
        assert match("新能源车", "新能源") is True        # 后缀剥离: 新能源车→新能源 (原行为保留)
        assert match("汽车", "汽车零部件") is True

    def test_suffix_words_not_overstripped(self):
        """旧字符集 rstrip 会把"机器人"剥空/"减速器"剥残 → 新实现核心词 ≥2 字."""
        from ai_stock.quant.agents.selection import (
            IndustryScanAgent,
            _strip_industry_suffix,
        )

        # 过度剥离防护: 剩余长度永远 ≥2, "机器人"不会塌缩成空/"机"
        assert _strip_industry_suffix("机器人") == "机器"
        assert _strip_industry_suffix("减速器") == "减速"
        assert _strip_industry_suffix("人形机器人") == "人形"
        # 核心词双向包含生效: 机器 与 机器视觉 匹配 (旧实现剥空后漏配)
        assert IndustryScanAgent._match_tag("机器视觉", "机器人") is True
        # 防误伤: 单字残余不作为核心词 ("电机"不会因剥出"电"误配"电力")
        assert IndustryScanAgent._match_tag("电力", "电机") is False

    def test_short_and_empty_names(self):
        from ai_stock.quant.agents.selection import IndustryScanAgent

        match = IndustryScanAgent._match_tag
        assert match("钢", "钢铁") is True                 # 短名走全名包含仍可匹配
        assert match("", "半导体") is False
        assert match("半导体", "") is False


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
