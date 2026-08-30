"""选股 Agent 集群 (设计文档 §7.1, 队列: selection_control).

自上而下投研流程 (Orchestrator FSM 驱动):
    selection_start → 宏观事件解析 → 涨停潮监控 → 行业生命周期定位 →
    个股三大支柱精选+深度分析(合并) → 入自选池

优化点 (选股效率与鲁棒性):
- 优化点 1: 个股精选与深度分析合并为每行业一次 LLM 调用, 批量输出
  "三大支柱评分 + 弹性 + 投资逻辑报告 + 置信度", 直接写入自选池;
  独立 deep_analysis 步骤已移除 (调用 18 次 → 约 5 次)
- 优化点 3: 阶段优选级由 LLM 结合市场定价状态动态加权, 静态表仅兜底;
  优化点 5: 涨停潮作为行业阶段修正器 (导入期强制升爆发前期)
- 优化点 4: 核心财务指标预提取表格化喂 LLM, 替代原始财报长文本 (省 Token)
- 优化点 6: 降级链 — 无事件→涨幅榜/资金异动量化扫描; 无选股→昨日自选池续持
- 优化点 7: 上报结果只携带关键结论, 完整明细仅落决策日志, 控制 Context 体积

原则:
- 宏观事件/政策驱动 → 产业生命周期定位 → 个股三大支柱精选 (§7.1.0)
- 硬过滤仍为硬门槛: ST/退市/北交所排除, 综合分低于门槛直接剔除;
  LLM 只生成事件解释、阶段判断、评分与置信度, 不参与布尔型硬门槛过滤
- 宁缺毋滥: 任一环节无合格候选 → 空结果继续流程, 不强行凑数
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from pydantic import BaseModel, Field

from .. import db_ops
from ..calendar_utils import get_trade_date
from ..config import (
    INDUSTRY_BOARD_SIZE,
    INDUSTRY_STOCK_POOL_SIZE,
    LEADER_STOCKS_PER_BOARD,
    MAX_INDUSTRIES_OUTPUT,
    MAX_LIMIT_UP_WAVES,
    MAX_SCAN_CANDIDATES,
    MIN_ENTRY_CONFIDENCE,
    MIN_STOCK_COMPREHENSIVE_SCORE,
    STOCK_SELECT_INDUSTRIES,
    STOCKS_PER_INDUSTRY,
)
from ..llm_helper import structured_invoke
from ..news_store import get_news_store
from ..orchestrator import get_orchestrator
from .base import BaseAgent

logger = logging.getLogger(__name__)

# 北交所代码前缀 (4/8/92) — 30% 涨跌幅、流动性差, 排除
_EXCLUDED_PREFIX = ("4", "8", "92")


def _is_excluded(code: str, name: str) -> bool:
    """ST / 退市风险 / 北交所排除."""
    code = str(code)
    name = str(name)
    return (
        code.startswith(_EXCLUDED_PREFIX)
        or "ST" in name.upper()
        or "退" in name
    )


def _parse_pct(value) -> float:
    """涨停股 zhangfu 字段 ('9.98%' / '9.98' / '-') → float."""
    try:
        text = str(value or "").replace("%", "").strip()
        return float(text) if text not in ("", "-", "None") else 0.0
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# LLM 输出 schema
# ---------------------------------------------------------------------------

# 产业生命周期阶段静态兜底优选级 (优化点 3):
# 仅在 LLM 未输出动态 priority (<=0) 时使用, 不再作为主排序逻辑;
# 动态加权由 LLM 结合当日量价/资金/事件持续性给出 (见 _lifecycle_evaluate)
STAGE_DEFAULT_PRIORITY = {
    "验证后期": 5,
    "爆发前期": 4,
    "爆发期": 3,
    "导入期": 2,
    "消化期": 1,
    "成熟期": 0,
    "衰退期": -1,
    "逻辑颠覆期": -2,
}
# 优化点 5: 涨停潮修正器强制升阶段后的优选级下限 (右侧确认信号)
_WAVE_PRIORITY_FLOOR = 8.0
# 优化点 7: 上报 Context 的阶段分析截断长度
_STAGE_ANALYSIS_CHARS = 150
# 生命周期诊断矩阵 (LLM Prompt 原文嵌入)
_LIFECYCLE_MATRIX = (
    "产业生命周期诊断矩阵:\n"
    "- 导入期: 技术未成熟, 无商业化落地 → 关注政策风向、技术突破概率\n"
    "- 爆发期: 需求爆发, 供不应求 → 关注产能扩张、订单增速\n"
    "- 验证期: 市场等待业绩兑现 → 关注量产进度、毛利率变化\n"
    "  (验证后期 = 业绩已开始兑现, 即将进入爆发, 为最优介入阶段)\n"
    "- 消化期: 产业逻辑不变, 但股价已提前透支 → 关注涨幅/时间/估值消化进度\n"
    "- 成熟期: 增速放缓, 行业洗牌 → 关注市场份额、成本控制\n"
    "- 衰退期: 需求萎缩, 行业整体承压 → 关注政策托底力度、转型方向\n"
    "- 逻辑颠覆期: 底层技术/商业模式被替代 → 关注新范式确定性、旧资产出清\n"
)


class IndustryLifecycleReport(BaseModel):
    """单个行业的生命周期阶段判断 (LLM 定性 + 动态加权)."""

    name: str = ""
    stage: str = "导入期"           # STAGE_DEFAULT_PRIORITY 八档之一 (验证期归入验证后期)
    analysis: str = ""
    # 动态优选级 0-10 (优化点 3): LLM 结合市场定价状态评估风险收益,
    # 同阶段行业可不同分; <=0 时代码侧回退静态兜底表
    priority: float = Field(default=0.0, ge=0, le=10)


class IndustryLifecycleList(BaseModel):
    industries: list[IndustryLifecycleReport] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 7.1.1 行业扫描与生命周期定位 Agent (重构: 宏观事件驱动)
# ---------------------------------------------------------------------------

class IndustryScanAgent(BaseAgent):
    """行业生命周期定位: 宏观事件映射行业池 → LLM 诊断矩阵定性 →
    阶段优选级排序 (涨停潮加分) → Top 4."""

    agent_name = "industry_scan"

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")

        events = list((context.get("macro_event") or {}).get("events") or [])
        wave_names = {
            w.get("name", "")
            for w in ((context.get("limit_up_monitor") or {}).get("waves") or [])
            if w.get("name")
        }

        boards = self.data.get_all_industries()
        candidates = self._collect_candidates(boards, events)
        if not candidates:
            result = {
                "decision": "empty",
                "trade_date": context.get("trade_date", get_trade_date()),
                "industries": [], "top_industries": [], "board_industries": [],
                "scanned": len(boards), "filtered_in": 0,
            }
            self.log(
                "scan_empty", reason="事件映射与涨幅榜均无候选行业, 宁缺毋滥",
                task_id=task_id, flow_id=flow_id,
            )
            self.report(flow_type, flow_id, step, result)
            return result

        if not self.llm.available:
            result = {
                "decision": "empty",
                "trade_date": context.get("trade_date", get_trade_date()),
                "industries": [], "top_industries": [], "board_industries": [],
                "scanned": len(candidates), "filtered_in": 0,
            }
            self.log(
                "scan_empty",
                reason="LLM 不可用, 无法完成生命周期定位, 宁缺毋滥",
                task_id=task_id, flow_id=flow_id,
            )
            self.report(flow_type, flow_id, step, result)
            return result

        stage_map = self._lifecycle_evaluate(candidates, events, wave_names)
        if not stage_map:
            result = {
                "decision": "empty",
                "trade_date": context.get("trade_date", get_trade_date()),
                "industries": [], "top_industries": [], "board_industries": [],
                "scanned": len(candidates), "filtered_in": 0,
            }
            self.log(
                "scan_empty", reason="生命周期诊断无有效输出, 宁缺毋滥",
                task_id=task_id, flow_id=flow_id,
            )
            self.report(flow_type, flow_id, step, result)
            return result

        # 动态优选级排序 (优化点 3) + 涨停潮阶段修正器 (优化点 5);
        # LLM 未覆盖的候选默认导入期 (不无声丢弃)
        industries: list[dict] = []
        stage_distribution: dict[str, int] = {}
        for cand in candidates:
            view = stage_map.get(cand["name"])
            stage = (view or {}).get("stage") or "导入期"
            if stage not in STAGE_DEFAULT_PRIORITY:
                stage = "导入期"
            llm_priority = float((view or {}).get("priority") or 0)
            # LLM 动态加权优先; 缺失时回退静态兜底表 (不静默丢弃)
            priority = (
                llm_priority if llm_priority > 0
                else float(STAGE_DEFAULT_PRIORITY[stage])
            )
            corrected = False
            in_wave = any(self._match_tag(cand["name"], w) for w in wave_names)
            if in_wave:
                # 涨停潮右侧确认: 导入期强制升爆发前期 (市场资金已完成基本面验证)
                if stage == "导入期":
                    stage = "爆发前期"
                    corrected = True
                    priority = max(priority, _WAVE_PRIORITY_FLOOR)
                priority += 1.0
            stage_distribution[stage] = stage_distribution.get(stage, 0) + 1
            industries.append({
                **cand,
                "stage": stage,
                "stage_corrected": corrected,
                "stage_analysis": (view or {}).get("analysis", ""),
                "priority": round(priority, 2),
            })
        industries.sort(key=lambda i: i["priority"], reverse=True)
        top = industries[:MAX_INDUSTRIES_OUTPUT]
        # 行业榜数据源: 同一排序的前 INDUSTRY_BOARD_SIZE 个行业 (前端行业榜),
        # 与继续个股精选的 Top N 同源, 由 stock_selection 步落库 (§7.1)
        board = industries[:INDUSTRY_BOARD_SIZE]

        # 优化点 7: 上报只携带关键结论 (丢弃涨跌家数等明细, 阶段分析截断)
        slim = [self._slim_industry(i) for i in top]
        result = {
            "decision": "ok",
            "trade_date": context.get("trade_date", get_trade_date()),
            "industries": slim,          # 兼容旧下游键名
            "top_industries": slim,
            "board_industries": [self._slim_industry(i) for i in board],
            "stage_distribution": stage_distribution,
            "scanned": len(boards),
            "filtered_in": len(candidates),
        }
        self.log(
            "scan_done",
            reason=(
                f"扫描 {len(boards)} 板块, 候选 {len(candidates)} 个, "
                f"生命周期分布 {stage_distribution}, "
                f"Top {len(top)}: "
                f"{[(i['name'], i['stage'], i['priority']) for i in top]}"
            ),
            task_id=task_id, flow_id=flow_id, detail=result,
        )
        self.report(flow_type, flow_id, step, result)
        return result

    @staticmethod
    def _slim_industry(ind: dict) -> dict:
        """Context 瘦身 (优化点 7): 丢弃当日涨跌明细字段, 阶段分析截断."""
        return {
            "code": ind.get("code", ""),
            "name": ind.get("name", ""),
            "board_level": ind.get("board_level", ""),
            "change_pct": ind.get("change_pct"),
            "main_net_inflow": ind.get("main_net_inflow"),
            "event_tag": ind.get("event_tag", ""),
            "stage": ind.get("stage", ""),
            "stage_corrected": ind.get("stage_corrected", False),
            "stage_analysis": (ind.get("stage_analysis") or "")[
                :_STAGE_ANALYSIS_CHARS
            ],
            "priority": ind.get("priority", 0),
        }

    # -- 候选行业池: 事件映射优先, 涨幅榜补足 ---------------------------------

    @staticmethod
    def _match_tag(board_name: str, tag: str) -> bool:
        """模糊匹配: 双向包含, 或核心词 (≥2字) 双向包含 (如 新能源车→汽车)."""
        if not board_name or not tag:
            return False
        if tag in board_name or board_name in tag:
            return True
        core = tag.rstrip("车机器材业链备件务网体化")
        return len(core) >= 2 and (core in board_name or board_name in core)

    def _collect_candidates(
        self, boards: list[dict], events: list[dict],
    ) -> list[dict]:
        """事件映射行业 ∪ 涨幅榜补足 → 候选板块 (去重, 上限内)."""
        by_code: dict[str, dict] = {
            b.get("code", ""): b for b in boards
            if b.get("code") and b.get("name")
        }
        tags: list[str] = []
        for evt in events:
            for tag in evt.get("impact_industries") or []:
                if tag and tag not in tags:
                    tags.append(tag)

        picked: dict[str, dict] = {}
        if tags:
            for code, board in by_code.items():
                name = board.get("name", "")
                matched = next(
                    (t for t in tags if self._match_tag(name, t)), "",
                )
                if matched:
                    picked[code] = {**board, "event_tag": matched}

        if len(picked) < MAX_SCAN_CANDIDATES:
            if not tags:
                # Level 2 降级 (优化点 6): 无事件映射时回退量化扫描 —
                # 涨幅榜前 10 ∪ 主力资金异动前 5, 保证无新闻时仍有候选
                for board in sorted(
                    by_code.values(),
                    key=lambda b: float(b.get("change_pct", 0) or 0),
                    reverse=True,
                )[:10]:
                    code = board.get("code", "")
                    if code not in picked:
                        picked[code] = {**board, "event_tag": ""}
                for board in sorted(
                    by_code.values(),
                    key=lambda b: float(b.get("main_net_inflow", 0) or 0),
                    reverse=True,
                )[:5]:
                    code = board.get("code", "")
                    if code not in picked and len(picked) < MAX_SCAN_CANDIDATES:
                        picked[code] = {**board, "event_tag": ""}
            else:
                # 有事件时用涨幅榜补足候选池 (§7.1.1)
                for board in sorted(
                    by_code.values(),
                    key=lambda b: float(b.get("change_pct", 0) or 0),
                    reverse=True,
                )[:MAX_SCAN_CANDIDATES]:
                    code = board.get("code", "")
                    if code in picked or len(picked) >= MAX_SCAN_CANDIDATES:
                        continue
                    picked[code] = {**board, "event_tag": ""}

        candidates: list[dict] = []
        for board in picked.values():
            detail = self.data.get_industry_detail(board.get("code", "")) or board
            candidates.append({
                "code": detail.get("code", board.get("code", "")),
                "name": detail.get("name", board.get("name", "")),
                "change_pct": float(detail.get("change_pct", 0) or 0),
                "main_net_inflow": round(
                    float(detail.get("main_net_inflow", 0) or 0), 0,
                ),
                "up_count": detail.get("up_count", 0),
                "down_count": detail.get("down_count", 0),
                "top_stock_name": detail.get("top_stock_name", ""),
                "top_stock_code": detail.get("top_stock_code", ""),
                "board_level": detail.get("board_level", ""),
                "event_tag": board.get("event_tag", ""),
            })
        return candidates[:MAX_SCAN_CANDIDATES]

    # -- LLM 生命周期诊断 ------------------------------------------------------

    def _lifecycle_evaluate(
        self, candidates: list[dict], events: list[dict], wave_names: set[str],
    ) -> dict[str, dict]:
        """LLM 按诊断矩阵逐行业定性 + 动态加权 (优化点 3/5); 失败返回空."""
        lines = []
        for c in candidates:
            inflow_yi = c["main_net_inflow"] / 1e8
            in_wave = any(self._match_tag(c["name"], w) for w in wave_names)
            lines.append(
                f"- {c['name']}({c['board_level']}): 当日涨幅 {c['change_pct']}%, "
                f"主力净流入 {inflow_yi:.2f} 亿, "
                f"上涨/下跌家数 {c['up_count']}/{c['down_count']}, "
                f"领涨股 {c['top_stock_name']}, "
                f"关联事件标签: {c['event_tag'] or '无'}, "
                f"今日有涨停潮: {'是' if in_wave else '否'}"
            )
        event_lines = "\n".join(
            f"- [{e.get('level', '')}] {e.get('title', '')} → "
            f"{', '.join(e.get('impact_industries') or [])}"
            for e in events[:10]
        ) or "(无)"
        prompt = (
            "你是 A 股行业策略分析师。请依据下方的产业生命周期诊断矩阵, "
            "结合当前重大事件与当日量价数据, 判断每个候选行业所处阶段, "
            "并独立评估其当前风险收益比 (动态优选级)。\n\n"
            f"{_LIFECYCLE_MATRIX}\n"
            "阶段取值必须为以下之一: "
            + " / ".join(STAGE_DEFAULT_PRIORITY) + "\n"
            "(处于验证期的行业请按业绩兑现进度归入 验证后期 或 消化期)\n\n"
            "动态加权要求 (勿机械套用静态阶段排序): 同一阶段在不同量价/资金/"
            "事件持续性下风险收益比差异很大, 请结合当日涨幅、主力净流入、"
            "涨跌家数、事件级别与持续性, 为每个行业输出 priority "
            "(0-10, 越高代表当前风险收益比越好)。\n"
            "特别规则: 若某行业今日出现批量涨停 (涨停潮), 请优先考虑将其阶段"
            "上调至爆发系 (爆发前期/爆发期) 并给予高 priority "
            "(市场资金已完成右侧确认)。\n\n"
            f"## 当前重大宏观事件\n{event_lines}\n\n"
            f"## 候选行业当日数据\n" + "\n".join(lines) + "\n\n"
            "逐行业输出 name / stage / analysis / priority "
            "(analysis 需给出阶段判断的核心依据, 如供需、政策、业绩兑现、"
            "估值透支情况)。"
        )
        try:
            report = structured_invoke(
                self.llm.pick(deep=True), IndustryLifecycleList, prompt,
                default=IndustryLifecycleList(), deep=True,
            )
        except Exception as exc:
            logger.warning("Industry lifecycle LLM report failed: %s", exc)
            return {}
        return {
            item.name: {
                "stage": item.stage,
                "analysis": item.analysis,
                "priority": float(item.priority or 0),
            }
            for item in report.industries if item.name
        }


# ---------------------------------------------------------------------------
# 7.1.2 涨停潮监控 Agent
# ---------------------------------------------------------------------------

class LimitUpMonitorAgent(BaseAgent):
    """涨停潮监控: 当日涨停股按题材归组, ≥3 只 → 涨停潮行业, 补充候选."""

    agent_name = "limit_up_monitor"
    MIN_LIMIT_UP_COUNT = 3          # 涨停潮判定阈值 (§7.1.2)

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")
        limit_ups = self.data.get_limit_up_stocks(days=1)

        # 过滤 ST / 退市 / 北交所
        valid = [
            s for s in limit_ups
            if not _is_excluded(s.get("code", ""), s.get("name", ""))
        ]
        # 按首个题材标签归组 (reason_tags 即涨停归因)
        groups: dict[str, list[dict]] = {}
        for stock in valid:
            tags = stock.get("reason_tags") or []
            key = tags[0] if tags else "未知题材"
            groups.setdefault(key, []).append(stock)

        waves: list[dict] = []
        waves_full: list[dict] = []   # 含样本明细, 仅供决策日志 (Context 瘦身)
        for theme, stocks in groups.items():
            if len(stocks) < self.MIN_LIMIT_UP_COUNT:
                continue
            changes = [_parse_pct(s.get("zhangfu")) for s in stocks]
            risk_tags = []
            if len(stocks) >= 8:
                risk_tags.append("涨停家数过多, 情绪过热")
            avg_turnover = sum(_parse_pct(s.get("huanshou")) for s in stocks) / len(stocks)
            if avg_turnover > 25:
                risk_tags.append("换手率过高, 纯情绪博弈")
            wave = {
                "name": theme,
                "limit_up_count": len(stocks),
                "avg_change_pct": round(sum(changes) / len(changes), 2) if changes else 0.0,
                "risk_tags": risk_tags,
            }
            waves.append(wave)
            waves_full.append({
                **wave,
                "samples": [
                    {"code": s.get("code", ""), "name": s.get("name", "")}
                    for s in stocks[:6]
                ],
            })

        # 涨停家数降序, 截断 (供 industry_scan 阶段修正/排序引用)
        waves.sort(key=lambda w: w["limit_up_count"], reverse=True)
        waves = waves[:MAX_LIMIT_UP_WAVES]
        waves_full.sort(key=lambda w: w["limit_up_count"], reverse=True)

        result = {
            "decision": "ok" if waves else "empty",
            "total_limit_up": len(valid),
            "waves": waves,
        }
        self.log(
            "wave_detected" if waves else "no_wave",
            reason=(
                f"当日有效涨停 {len(valid)} 只, 识别涨停潮行业 "
                f"{len(waves)} 个: {[w['name'] for w in waves]}"
            ),
            task_id=task_id, flow_id=flow_id,
            detail={**result, "waves": waves_full},
        )
        self.report(flow_type, flow_id, step, result)
        return result


# ---------------------------------------------------------------------------
# 7.1.3 个股精选 + 深度分析合并 Agent (优化点 1/4/6)
# ---------------------------------------------------------------------------

class StockDeepEval(BaseModel):
    """单只个股的合并评估 (优化点 1): 三大支柱 + 弹性 + 投资逻辑 + 置信度,
    一次 LLM 调用完成原精选+深度分析两项职责."""

    code: str = ""
    name: str = ""
    score: float = Field(default=0.0, ge=0, le=10)          # 三大支柱综合 0-10
    elasticity_score: float = Field(default=0.0, ge=0, le=10)  # 弹性 0-10
    reason: str = ""                                         # 入选理由 (投资逻辑核心)
    bull_factors: list[str] = Field(default_factory=list)    # 主要利多 (尽量具体)
    bear_factors: list[str] = Field(default_factory=list)    # 主要利空 (重点输出)
    stage_judgement: str = ""                                # 当前所处阶段判断
    rise_trigger: str = ""                                   # 上涨触发条件
    confidence: float = Field(default=0.0, ge=0, le=10)      # 综合置信度 (入池门槛)
    risk_tags: list[str] = Field(default_factory=list)


class StockDeepEvalList(BaseModel):
    stocks: list[StockDeepEval] = Field(default_factory=list)


# 三大支柱 + 弹性评估标准 (LLM Prompt 原文嵌入)
_THREE_PILLARS_CRITERIA = (
    "三大支柱评估标准 (每项 0-10, 综合得分取三者的权衡):\n"
    "① 行业地位与竞争格局: 市场份额、产业链话语权、竞争格局优劣\n"
    "② 技术壁垒与护城河: 专利/研发、成本/品牌/网络效应, 壁垒趋势变深或变浅\n"
    "③ 基本面财务健康度: 成长性、ROE/净利率、经营现金流/净利润匹配度、"
    "资产负债率、估值与成长性匹配度\n\n"
    "弹性评估标准 (单独打分 0-10): 中小市值 (300 亿以下加分)、高换手率、"
    "高量比、近期涨幅适中未透支。\n"
)


class StockSelectionAgent(BaseAgent):
    """Top 行业成分股合并评估: 三大支柱 + 弹性 + 投资逻辑 + 置信度,
    每行业一次批量 LLM 调用 (优化点 1), 双门槛 (综合分 + 置信度) 直接入自选池;
    每行业选综合分最高的 3 只 (同分按弹性)。

    附带职责: 把行业扫描的前 ``INDUSTRY_BOARD_SIZE`` 个行业落库为行业榜,
    每行业附前 ``LEADER_STOCKS_PER_BOARD`` 只龙头股 (精选行业用合并评估
    排序, 其余行业取流动性前 N 成分股)。
    降级 (优化点 6): 无合格选股时回退昨日自选池续持推荐。
    """

    agent_name = "stock_selection"

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")

        scan = context.get("industry_scan") or {}
        board_industries = list(scan.get("board_industries") or [])
        trade_date = (
            scan.get("trade_date")
            or context.get("trade_date")
            or get_trade_date()
        )

        industries = list(
            scan.get("top_industries")
            or scan.get("industries")
            or []
        )
        if not industries:
            self._persist_industry_board(board_industries, {}, trade_date)
            result = self._fallback_result(
                "无生命周期优选行业, 宁缺毋滥", task_id, flow_id,
            )
            self.report(flow_type, flow_id, step, result)
            return result

        selected: list[dict] = []
        dropped_low = 0
        evaluated = 0
        # 行业名 → 龙头股列表 (合并评估全量排序), 供行业榜落库引用;
        # 优化点 1: 仅前 STOCK_SELECT_INDUSTRIES 个行业做 LLM 深评,
        # 其余行业行业榜取流动性前 N 成分股
        board_leaders: dict[str, list[dict]] = {}
        for industry in industries[:STOCK_SELECT_INDUSTRIES]:
            picks, dropped, evaluated_n, leaders = self._select_in_industry(industry)
            selected.extend(picks)
            dropped_low += dropped
            evaluated += evaluated_n
            board_leaders[industry.get("name", "")] = leaders

        # 行业榜落库: 前 10 行业 + 每行业前 10 龙头股 (替代旧新闻热度榜)
        self._persist_industry_board(board_industries, board_leaders, trade_date)

        # 优化点 6 Level 3: 无合格选股 → 昨日自选池续持推荐 (不重复写库)
        if not selected:
            result = self._fallback_result(
                "各行业均无合格选股, 宁缺毋滥", task_id, flow_id,
            )
            self.report(flow_type, flow_id, step, result)
            return result

        # 入池 (优化点 1: 合并深析直接写库, 替代独立 deep_analysis 步):
        # 综合分门槛已在精选时剩除, 此处叠加置信度门槛 (双门槛)
        pool_written = 0
        skipped_conf = 0
        for pick in selected:
            if float(pick.get("confidence", 0) or 0) < MIN_ENTRY_CONFIDENCE:
                skipped_conf += 1
                self.log(
                    "skip_low_confidence", symbol=pick["symbol"],
                    reason=f"置信度 {pick.get('confidence')} 低于门槛 "
                           f"{MIN_ENTRY_CONFIDENCE}",
                    task_id=task_id, flow_id=flow_id,
                )
                continue
            try:
                db_ops.upsert_optional_stock(self._pool_entry(pick))
                self._ingest_news(pick["symbol"])
            except Exception as exc:
                logger.warning(
                    "Optional pool upsert failed for %s: %s",
                    pick["symbol"], exc,
                )
                continue
            pool_written += 1
            self.log(
                "pool_added", symbol=pick["symbol"],
                reason=f"入池: {pick.get('reason', '')[:200]}",
                task_id=task_id, flow_id=flow_id,
                detail={
                    "confidence": pick.get("confidence"),
                    "bear_factors": pick.get("bear_factors"),
                    "risk_tags": pick.get("risk_tags"),
                },
            )

        # 优化点 7: 上报只携带关键结论, 完整明细落决策日志
        result = {
            "decision": "ok",
            "selected": [self._slim_pick(p) for p in selected],
            "evaluated": evaluated,
            "dropped_low_score": dropped_low,
            "skipped_low_confidence": skipped_conf,
            "pool_written": pool_written,
        }
        self.log(
            "select_done",
            reason=(
                f"{min(len(industries), STOCK_SELECT_INDUSTRIES)} 个行业评估 "
                f"{evaluated} 只, 选出 {len(selected)} 只, 入池 {pool_written} 只, "
                f"综合分不足(<{MIN_STOCK_COMPREHENSIVE_SCORE})剔除 {dropped_low} 只, "
                f"置信度不足(<{MIN_ENTRY_CONFIDENCE})剔除 {skipped_conf} 只: "
                f"{[s['symbol'] for s in selected]}"
            ),
            task_id=task_id, flow_id=flow_id,
            detail={**result, "selected": selected},
        )
        self.report(flow_type, flow_id, step, result)
        return result

    # -- 降级与入池辅助 (优化点 6/1) -------------------------------------------

    @staticmethod
    def _slim_pick(pick: dict) -> dict:
        """Context 瘦身 (优化点 7): 上报只保留关键结论字段."""
        return {
            "symbol": pick.get("symbol", ""),
            "name": pick.get("name", ""),
            "industry": pick.get("industry", ""),
            "score": pick.get("score"),
            "confidence": pick.get("confidence"),
        }

    def _fallback_result(
        self, reason: str, task_id: str, flow_id: str,
    ) -> dict:
        """优化点 6 Level 3 降级: 全流程无选股 → 自选池续持推荐."""
        held = self._fallback_optional_pool()
        base = {"evaluated": 0, "dropped_low_score": 0,
                "skipped_low_confidence": 0, "pool_written": 0}
        if not held:
            result = {"decision": "empty", "selected": [], **base}
            self.log(
                "select_empty", reason=reason,
                task_id=task_id, flow_id=flow_id,
            )
            return result
        result = {
            "decision": "fallback",
            "selected": [self._slim_pick(p) for p in held],
            **base,
        }
        self.log(
            "select_fallback",
            reason=f"{reason}; Level 3 降级: 续持现有自选池 {len(held)} 只",
            task_id=task_id, flow_id=flow_id,
            detail={**result, "selected": held},
        )
        return result

    @staticmethod
    def _fallback_optional_pool() -> list[dict]:
        """Level 3 降级数据源: 现有自选池 (active) 按置信度降序,
        仅作续持推荐输出, 不重复写库."""
        try:
            pool = db_ops.get_optional_pool(status="active")
        except Exception as exc:
            logger.warning("Optional pool fallback read failed: %s", exc)
            return []
        pool.sort(
            key=lambda e: float(e.get("confidence") or 0), reverse=True,
        )
        return [
            {
                "symbol": e.get("symbol", ""),
                "name": e.get("name", ""),
                "industry": e.get("industry", ""),
                "score": None,
                "confidence": round(float(e.get("confidence") or 0), 1),
                "reason": "续持推荐 (降级兑底: 现有自选池未移出标的)",
                "fallback": True,
            }
            for e in pool if e.get("symbol")
        ]

    @staticmethod
    def _pool_entry(pick: dict) -> dict:
        """自选池落库结构 (与原 deep_analysis 入库口径一致)."""
        bull = [str(f) for f in (pick.get("bull_factors") or [])]
        bear = [str(f) for f in (pick.get("bear_factors") or [])]
        return {
            "symbol": pick.get("symbol", ""),
            "name": pick.get("name", ""),
            "industry": pick.get("industry", ""),
            "reason": pick.get("reason", ""),
            "bull_factors": bull,
            "bear_factors": bear,
            "stage_judgement": pick.get("stage_judgement", ""),
            "rise_trigger": pick.get("rise_trigger", ""),
            "risk_tags": pick.get("risk_tags") or [],
            "confidence": float(pick.get("confidence", 0) or 0),
            "report": (
                f"入选理由: {pick.get('reason', '')}\n"
                f"利多: {'; '.join(bull)}\n利空: {'; '.join(bear)}\n"
                f"阶段: {pick.get('stage_judgement', '')}\n"
                f"上涨触发: {pick.get('rise_trigger', '')}"
            ),
        }

    def _ingest_news(self, symbol: str) -> None:
        """个股近 72h 新闻入向量库 (崩塌检测/清仓决策数据源); 失败不阻断."""
        try:
            news = self.data.get_stock_news(symbol, hours=72)
        except Exception as exc:
            logger.warning("stock news fetch failed for %s: %s", symbol, exc)
            return
        if not news:
            return
        try:
            get_news_store().add_news(news, symbol=symbol)
        except Exception as exc:
            logger.warning("news store add failed for %s: %s", symbol, exc)

    # -- 行业内候选与评估 --------------------------------------------------

    def _select_in_industry(
        self, industry: dict,
    ) -> tuple[list[dict], int, int, list[dict]]:
        """单行业选股: 成分股池 → 合并深析 LLM 评估 → 硬门槛过滤 → Top 3.

        返回 (picks, dropped, evaluated_n, board_leaders):
        board_leaders = 全部受评股按 (综合分, 弹性) 降序的前 N 只,
        供行业榜龙头股展示 (不受入池门槛限制)。
        """
        code = industry.get("code", "")
        if not code:
            return [], 0, 0, []
        candidates = self._industry_candidates(code)
        if not candidates:
            return [], 0, 0, []
        profiles = self._stock_profiles(candidates)
        if not profiles:
            return [], 0, 0, []

        evals = self._evaluate_stocks(industry, profiles)
        by_code = {p["code"]: p for p in profiles}
        dropped = 0
        qualified: list[dict] = []
        merged_all: list[dict] = []
        for ev in evals:
            cand = by_code.get(ev.get("code", ""))
            if cand is None:
                continue  # LLM 虚构代码, 丢弃 (硬过滤, 非评分)
            merged = {**cand, **ev}
            merged_all.append(merged)
            if float(ev.get("score", 0) or 0) < MIN_STOCK_COMPREHENSIVE_SCORE:
                dropped += 1   # 综合分硬门槛: 宁缺毋滥直接剔除并计数 (§7.2)
                continue
            qualified.append(merged)

        # 按综合分降序, 同分按弹性分降序, 每行业取前 3 只 (§7.2)
        qualified.sort(
            key=lambda c: (
                float(c.get("score", 0)), float(c.get("elasticity_score", 0)),
            ),
            reverse=True,
        )
        picks = []
        for cand in qualified[:STOCKS_PER_INDUSTRY]:
            base_risk = self._risk_tags(cand)
            llm_risk = [str(t) for t in (cand.get("risk_tags") or [])]
            picks.append({
                "symbol": cand["code"],
                "name": cand["name"],
                "industry": industry.get("name", ""),
                "industry_source": "board",
                "industry_stage": industry.get("stage", ""),
                "event_tag": industry.get("event_tag", ""),
                "score": round(float(cand.get("score", 0) or 0), 2),
                "elasticity_score": round(
                    float(cand.get("elasticity_score", 0) or 0), 2,
                ),
                "confidence": round(float(cand.get("confidence", 0) or 0), 1),
                "reason": cand.get("reason", ""),
                "bull_factors": [
                    str(f) for f in (cand.get("bull_factors") or [])
                ],
                "bear_factors": [
                    str(f) for f in (cand.get("bear_factors") or [])
                ],
                "stage_judgement": cand.get("stage_judgement", ""),
                "rise_trigger": cand.get("rise_trigger", ""),
                "risk_tags": sorted(set(base_risk + llm_risk)),
            })

        # 行业榜龙头股: 全部受评股排序取前 N, 精选入池的打 "精选" 标 (§7.1)
        merged_all.sort(
            key=lambda c: (
                float(c.get("score", 0) or 0),
                float(c.get("elasticity_score", 0) or 0),
            ),
            reverse=True,
        )
        picked_codes = {p["symbol"] for p in picks}
        board_leaders = [
            {
                "code": cand["code"],
                "name": cand.get("name", ""),
                "change_pct": float(cand.get("change_pct", 0) or 0),
                "turnover_rate": float(cand.get("turnover_rate", 0) or 0),
                "score": round(float(cand.get("score", 0) or 0), 2),
                "leader_label": "精选" if cand["code"] in picked_codes else "",
            }
            for cand in merged_all[:LEADER_STOCKS_PER_BOARD]
        ]
        return picks, dropped, len(profiles), board_leaders

    def _industry_candidates(self, industry_code: str) -> list[dict]:
        """行业成分股池: 流动性前 N 只, 硬过滤 ST/退市/北交所."""
        stocks = self.data.get_industry_stocks(
            industry_code, top_n=INDUSTRY_STOCK_POOL_SIZE,
        )
        return [
            dict(s) for s in stocks
            if not _is_excluded(s.get("code", ""), s.get("name", ""))
        ]

    # -- 行业榜落库 ----------------------------------------------------------

    def _persist_industry_board(
        self,
        board_industries: list[dict],
        board_leaders: dict[str, list[dict]],
        trade_date: str,
    ) -> None:
        """行业榜写入: 生命周期排序前 INDUSTRY_BOARD_SIZE 个行业,
        每行业附前 LEADER_STOCKS_PER_BOARD 只龙头股。
        精选行业用三大支柱评估排序; 其余行业取流动性前 N 成分股。失败不阻断主流程。"""
        rows: list[dict] = []
        for idx, ind in enumerate(board_industries[:INDUSTRY_BOARD_SIZE], 1):
            name = ind.get("name", "")
            leaders = board_leaders.get(name)
            if leaders is None:
                leaders = self._constituent_leaders(ind.get("code", ""))
            rows.append({
                "rank": idx,
                "industry": name,
                "industry_code": ind.get("code", ""),
                "industry_level": ind.get("board_level", ""),
                "stage": ind.get("stage", ""),
                "event_tag": ind.get("event_tag", ""),
                "heat_score": float(ind.get("priority", 0) or 0),
                "change_pct": ind.get("change_pct"),
                "main_net_inflow": ind.get("main_net_inflow"),
                "leader_stocks": leaders,
            })
        if not rows:
            return
        try:
            saved = db_ops.save_industry_board(trade_date, rows)
        except Exception as exc:
            logger.warning("Industry board persist failed: %s", exc)
            return
        logger.info("Industry board saved: %s, %d industries", trade_date, saved)

    def _constituent_leaders(self, industry_code: str) -> list[dict]:
        """未经精选覆盖行业的龙头股: 流动性前 N 成分股 (硬过滤 ST/退市/北交所)."""
        if not industry_code:
            return []
        try:
            stocks = self.data.get_industry_stocks(
                industry_code, top_n=LEADER_STOCKS_PER_BOARD,
            )
        except Exception as exc:
            logger.warning(
                "Leader constituents fetch failed for %s: %s", industry_code, exc,
            )
            return []
        valid = [
            s for s in stocks
            if not _is_excluded(s.get("code", ""), s.get("name", ""))
        ][:LEADER_STOCKS_PER_BOARD]
        return [
            {
                "code": s.get("code", ""),
                "name": s.get("name", ""),
                "change_pct": float(s.get("change_pct", 0) or 0),
                "turnover_rate": float(s.get("turnover_rate", 0) or 0),
                "score": None,
                "leader_label": "领涨" if idx == 0 else "",
            }
            for idx, s in enumerate(valid)
        ]

    def _stock_profiles(self, candidates: list[dict]) -> list[dict]:
        """候选股实时行情 + 预提取核心财务指标 (优化点 4: 表格化喂 LLM)."""
        profiles: list[dict] = []
        for cand in candidates[:INDUSTRY_STOCK_POOL_SIZE]:
            code = cand.get("code", "")
            quote = None
            try:
                quote = self.data.get_realtime_quote(code)
            except Exception as exc:
                logger.warning("quote fetch failed for %s: %s", code, exc)
            fundamentals = ""
            try:
                fundamentals = self.data.get_fundamentals_text(code) or ""
            except Exception as exc:
                logger.warning("fundamentals fetch failed for %s: %s", code, exc)
            profiles.append({
                "code": code,
                "name": cand.get("name", "") or (quote or {}).get("name", ""),
                "change_pct": float(
                    (quote or {}).get("change_pct")
                    if quote else cand.get("change_pct", 0)
                    or 0
                ),
                "turnover_rate": float(cand.get("turnover_rate", 0) or 0),
                "volume_ratio": float(cand.get("volume_ratio", 0) or 0),
                "market_cap": float(
                    (quote or {}).get("market_cap")
                    if quote else cand.get("market_cap", 0)
                    or 0
                ),
                "amount": float(cand.get("amount", 0) or 0),
                "main_net_inflow": float(cand.get("main_net_inflow", 0) or 0),
                "fund_metrics": self._extract_fund_metrics(fundamentals),
            })
        return profiles

    # 优化点 4: 从基本面文本预提取核心估值/财务指标 (不新增数据源);
    # 标签格式与 get_fundamentals (腾讯行情 + mootdx + 同花顺预期) 对齐
    _FUND_METRIC_PATTERNS: tuple[tuple[str, str], ...] = (
        ("PE(TTM)", r"PE \(TTM\):\s*([-+]?\d+(?:\.\d+)?)"),
        ("PB", r"PB:\s*([-+]?\d+(?:\.\d+)?)"),
        ("ROE%", r"ROE \(%\):\s*([-+]?\d+(?:\.\d+)?)"),
        ("PEG", r"PEG:\s*([-+]?\d+(?:\.\d+)?)"),
        ("FwdPE", r"Forward PE \(FY(\d+)\):\s*([-+]?\d+(?:\.\d+)?)"),
        ("市值亿", r"Market Cap \(100M CNY\):\s*([-+]?\d+(?:\.\d+)?)"),
    )

    @classmethod
    def _extract_fund_metrics(cls, text: str) -> str:
        """基本面长文本 → 紧凑指标行 (省 Token, 提取失败返回空)."""
        if not text:
            return ""
        parts: list[str] = []
        for label, pattern in cls._FUND_METRIC_PATTERNS:
            m = re.search(pattern, text)
            if not m:
                continue
            if label == "FwdPE":
                parts.append(f"FwdPE(FY{m.group(1)})={m.group(2)}")
            else:
                parts.append(f"{label}={m.group(1)}")
        return ", ".join(parts)

    def _build_deep_prompt(
        self, industry: dict, profiles: list[dict],
    ) -> str:
        # 优化点 4: 指标表格化, 不再投喂财报长文本原文
        rows = [
            "| 代码 | 名称 | 当日涨幅% | 换手率% | 量比 | 市值(亿) | 核心财务指标 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for p in profiles:
            mcap_yi = float(p.get("market_cap", 0) or 0) / 1e8
            rows.append(
                f"| {p['code']} | {p.get('name', '')} | "
                f"{float(p.get('change_pct', 0) or 0):.2f} | "
                f"{float(p.get('turnover_rate', 0) or 0):.2f} | "
                f"{float(p.get('volume_ratio', 0) or 0):.2f} | "
                f"{mcap_yi:.1f} | {p.get('fund_metrics', '') or '-'} |"
            )
        table = "\n".join(rows)
        return (
            f"你是 A 股行业研究员。行业: {industry.get('name', '')} "
            f"(生命周期阶段: {industry.get('stage', '未知')}, "
            f"关联事件: {industry.get('event_tag') or '无'})。\n"
            "请对该行业成分股批量完成深度评估: 三大支柱综合评分 + 弹性评分 + "
            "完整投资逻辑 + 置信度 (一次输出到位, 无第二轮深析)。\n\n"
            f"{_THREE_PILLARS_CRITERIA}\n"
            "评分要求: score 为三大支柱综合得分 0-10 (宁缺毋滥, 基本面明显恶化"
            "/估值严重透支应给低分); elasticity_score 单独评估弹性 0-10; "
            "reason 为入选理由 (投资逻辑核心, 2-3 句); "
            "bull_factors 主要利多 (尽量具体); bear_factors 主要利空 (重点输出); "
            "stage_judgement 为该股当前所处阶段 (如行业爆发前夕/大盘结构性调整"
            "/技术回调); rise_trigger 为上涨触发条件; "
            "confidence 为综合置信度 0-10 (宁缺毋滥请从严); "
            "risk_tags 风险标签列表。\n"
            "只评估下方给出的股票, 不要虚构代码。\n\n"
            f"## 股票数据 (Markdown 表格)\n{table}\n\n"
            "输出 stocks 列表, 每项包含 code / name / score / elasticity_score / "
            "reason / bull_factors / bear_factors / stage_judgement / "
            "rise_trigger / confidence / risk_tags。"
        )

    def _evaluate_stocks(
        self, industry: dict, profiles: list[dict],
    ) -> list[dict]:
        """LLM 合并深析评估 (优化点 1); 不可用/失败返回空 (宁缺毋滥)."""
        if not self.llm.available:
            return []
        prompt = self._build_deep_prompt(industry, profiles)
        try:
            report = structured_invoke(
                self.llm.pick(deep=True), StockDeepEvalList, prompt,
                default=StockDeepEvalList(), deep=True,
            )
        except Exception as exc:
            logger.warning(
                "Stock deep eval failed for %s: %s",
                industry.get("name", ""), exc,
            )
            return []
        return [
            {
                "code": s.code, "name": s.name,
                "score": float(s.score or 0),
                "elasticity_score": float(s.elasticity_score or 0),
                "reason": s.reason,
                "bull_factors": list(s.bull_factors or []),
                "bear_factors": list(s.bear_factors or []),
                "stage_judgement": s.stage_judgement,
                "rise_trigger": s.rise_trigger,
                "confidence": round(float(s.confidence or 0), 1),
                "risk_tags": list(s.risk_tags or []),
            }
            for s in report.stocks if s.code
        ]

    @staticmethod
    def _risk_tags(cand: dict) -> list[str]:
        """结构化行情硬指标风险标签 (与 LLM 标签合并)."""
        tags: list[str] = []
        change = float(cand.get("change_pct", 0) or 0)
        turnover = float(cand.get("turnover_rate", 0) or 0)
        if change >= 9.8:
            tags.append("当日涨停, 追高风险")
        elif change >= 5.0:
            tags.append("当日涨幅较大")
        if turnover >= 20:
            tags.append("换手率过高")
        mcap = float(cand.get("market_cap", 0) or 0)
        if 0 < mcap < 5e9:
            tags.append("小市值波动大")
        return tags


# ---------------------------------------------------------------------------
# selection_start: 调度入口 → 启动 FSM 流程 (深度分析已合并入 stock_selection)
# ---------------------------------------------------------------------------

def handle_selection_start(task: dict) -> dict:
    """selection_control 队列的 selection_start handler: 启动选股流程 (幂等)."""
    payload = task.get("payload", {})
    trade_date = payload.get("trade_date", get_trade_date())
    # 幂等 flow_id: 同一交易日同一时点只跑一次 (调度 idempotent_key 已保证)
    trigger = payload.get("trigger", datetime.now().strftime("%H%M"))
    flow_id = f"selection_{trade_date}_{trigger}"
    return get_orchestrator().start_flow(
        "selection",
        flow_id=flow_id,
        data={"trade_date": trade_date, "trigger": trigger},
    )
