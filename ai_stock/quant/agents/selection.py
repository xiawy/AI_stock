"""选股 Agent 集群 (设计文档 §7.1, 队列: selection_control).

自上而下投研流程 (Orchestrator FSM 驱动):
    selection_start → 宏观事件解析 → 涨停潮监控 → 行业生命周期定位 →
    个股三大支柱精选+深度分析(合并) → 入自选池

优化点 (选股效率与鲁棒性):
- 优化点 1: 个股精选与深度分析合并为每行业一次 LLM 调用, 批量输出
  "三大支柱评分 + 弹性 + 投资逻辑报告 + 置信度", 直接写入自选池;
  独立 deep_analysis 步骤已移除 (调用 18 次 → 约 5 次)
- 优化点 3: 行业综合优先级由代码侧加权公式计算 (供需失衡分×0.40 +
  生命周期阶段分×0.30 + 上游传导加分×0.15 + 涨停潮加分×0.15 +
  扩产周期/进入壁垒微调), 阶段静态表提供阶段分项, 不再依赖 LLM 直接打分;
  优化点 5: 涨停潮作为右侧确认信号并入加权加分; 供需严重失衡且阶段分非正时,
  强制将阶段上调为爆发前期 (供需第一性原理 × 生命周期矩阵 × 上游传导三位一体)
- 上游传导二次验证: Top 行业点名的上游未上排名榜时, 能匹配候选池/板块库则
  附加展示在行业榜 (不入 Top, 不参与个股精选); 无法验证的上游 (LLM 幻觉) 丢弃
- 优化点 4: 核心财务指标预提取表格化喂 LLM, 替代原始财报长文本 (省 Token)
- 优化点 6: 降级链 — 无事件→涨幅榜/资金异动量化扫描; 无选股→昨日自选池续持
- 优化点 7: 上报结果只携带关键结论, 完整明细仅落决策日志, 控制 Context 体积
- 时间感知增强: 生命周期 Prompt 明示当前评估日期, 基本面指标行携带最新报告期,
  防止 LLM 凭题材惯性/滞后旧财报误判"业绩验证"阶段

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
    BONUS_UPSTREAM,
    BONUS_WAVE,
    IMBALANCE_STAGE_ESCALATION,
    INDUSTRY_BOARD_SIZE,
    INDUSTRY_STOCK_POOL_SIZE,
    LEADER_STOCKS_PER_BOARD,
    MAX_INDUSTRIES_OUTPUT,
    MAX_LIMIT_UP_WAVES,
    MAX_SCAN_CANDIDATES,
    MAX_TRANSMISSION_BOARDS,
    MIN_ENTRY_CONFIDENCE,
    MIN_STOCK_COMPREHENSIVE_SCORE,
    STOCK_SELECT_INDUSTRIES,
    STOCKS_PER_INDUSTRY,
    TRANSMISSION_DECAY,
    WEIGHT_BARRIER,
    WEIGHT_CYCLE,
    WEIGHT_IMBALANCE,
    WEIGHT_STAGE,
    WEIGHT_UPSTREAM,
    WEIGHT_WAVE,
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


# 行业名核心词匹配的后缀剥离表: 长后缀优先, 逐词剥离且保证剩余 ≥2 字.
# 替代旧字符集 rstrip("车机器材业链备件务网体化"): 旧实现会把"机器人"/"减速器"
# 这类词整体剥空/剥残, 核心词塌缩后退化为纯全名包含匹配 (漏配风险).
_TAG_SUFFIXES = (
    "产业链", "概念", "设备", "材料", "制造", "加工", "服务", "行业", "产业",
    "化", "业", "链", "件", "备", "材", "器", "机", "车", "人",
)


def _strip_industry_suffix(name: str) -> str:
    """行业名后缀剥离 → 核心词; 每步保证剩余长度 ≥2, 避免过度剥离塌缩."""
    core = name
    while len(core) > 2:
        for suf in _TAG_SUFFIXES:
            if core.endswith(suf) and len(core) - len(suf) >= 2:
                core = core[: -len(suf)]
                break
        else:
            break
    return core


# ---------------------------------------------------------------------------
# LLM 输出 schema
# ---------------------------------------------------------------------------

# 产业生命周期阶段静态分表: 综合排名公式中的阶段分项 (权重 WEIGHT_STAGE),
# 归一化口径 stage_score / 5.0 (最高 验证后期 5 分); 负分表达成熟/衰退/
# 逻辑颠覆阶段的惩罚; 供需严重失衡时代码侧可强制上调阶段 (见 handle)
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
# 扩产周期微调: 周期越长, 供给缺口越难弥合 → 供需逻辑越持久 (加分)
_CYCLE_MODIFIER = {"长期": 0.0, "中期": -0.5, "短期": -1.0}
# 进入壁垒微调: 壁垒越高, 存量厂商超额利润越难被新进入者稀释 (加分)
_BARRIER_MODIFIER = {"极高": 0.5, "高": 0.0, "中": -0.5, "低": -1.0}
# 事件类型中文标签 (macro_event 侧定义, 仅作候选行提示文案, 不参与评分)
_EVENT_TYPE_LABELS = {
    "first_proposal": "首次提出",
    "policy_support": "政策支持",
    "tech_breakthrough": "技术突破",
}
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
    """单个行业的生命周期阶段判断 + 供需第一性原理分析 (LLM 结构化输出).

    供需 × 矩阵 × 上游传导三位一体: LLM 只输出分项判断 (阶段/供需失衡分/
    扩产周期/进入壁垒/上游传导), 综合优先级由代码侧加权公式计算, 不依赖
    LLM 直接打分 (priority 字段仅为向后兼容保留).
    """

    name: str = ""
    # ---- 原有字段 (保留, 向后兼容) ----
    stage: str = "导入期"           # STAGE_DEFAULT_PRIORITY 八档之一 (验证期归入验证后期)
    analysis: str = ""              # 综合判断依据 (150 字以内)
    priority: float = Field(default=0.0, ge=0, le=10)  # 兼容保留: 现由代码侧计算
    # ---- 新增: 供需第一性原理分项 ----
    demand_driver: str = ""         # 需求驱动因素 (如 AI 服务器拉动高频高速 PCB 需求)
    supply_constraint: str = ""     # 供给约束因素 (如扩产谨慎/环保限产/技术壁垒)
    # 供需失衡分: 10=极度供不应求, 0=极度供过于求 (无明显失衡应给 3 分以下)
    imbalance_score: float = Field(default=0.0, ge=0, le=10)
    expansion_cycle: str = ""       # 扩产周期: 短期(<6个月) / 中期(6-18个月) / 长期(>18个月)
    entry_barrier: str = ""         # 进入壁垒: 极高 / 高 / 中 / 低
    # ---- 新增: 上游传导发散 (快人一步的关键, 被点名行业排序加分) ----
    transmission_upstream: list[str] = Field(default_factory=list)
    transmission_evidence: str = ""  # 传导证据 (涨价函/产能公告等, 无证据应留空)


class IndustryLifecycleList(BaseModel):
    industries: list[IndustryLifecycleReport] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 7.1.1 行业扫描与生命周期定位 Agent (重构: 宏观事件驱动)
# ---------------------------------------------------------------------------

class IndustryScanAgent(BaseAgent):
    """行业生命周期定位 + 供需第一性原理分析: 宏观事件映射行业池 →
    LLM 供需 × 矩阵 × 上游传导三位一体定性 → 加权综合优先级排序 → Top 4.

    综合分 = 供需失衡分×40% + 阶段分×30% + 上游传导加分×15% +
    涨停潮加分×15% + 扩产周期/进入壁垒微调, 映射到 0-10.
    上游传导二次验证: Top 行业点名的上游未上榜时, 能对账板块库则附加展示在行业榜.
    """

    agent_name = "industry_scan"

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")
        trade_date = context.get("trade_date", get_trade_date())

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
                "trade_date": trade_date,
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
                "trade_date": trade_date,
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

        # 事件类型映射 (如 first_proposal): 仅作生命周期 Prompt 的新主题提示,
        # 不参与加权评分 (轻量级增强: "首次提出→业绩验证"闭环)
        event_type_map: dict[str, str] = {}
        for ev in events:
            for ind in ev.get("impact_industries") or []:
                if ind:
                    event_type_map.setdefault(ind, ev.get("event_type", ""))
        stage_map = self._lifecycle_evaluate(
            candidates, events, wave_names, event_type_map, trade_date,
        )
        if not stage_map:
            result = {
                "decision": "empty",
                "trade_date": trade_date,
                "industries": [], "top_industries": [], "board_industries": [],
                "scanned": len(candidates), "filtered_in": 0,
            }
            self.log(
                "scan_empty", reason="生命周期诊断无有效输出, 宁缺毋滥",
                task_id=task_id, flow_id=flow_id,
            )
            self.report(flow_type, flow_id, step, result)
            return result

        # 加权综合排名 (优化点 3/5): 供需失衡分×40% + 阶段分×30% +
        # 上游传导加分×15% + 涨停潮加分×15% + 扩产周期/进入壁垒微调;
        # LLM 未覆盖的候选默认导入期 (不无声丢弃)
        # 第一遍: 收集所有被点名的上游行业 (上游传导发散加分依据)
        upstream_set: set[str] = set()
        for cand in candidates:
            for up in (stage_map.get(cand["name"]) or {}).get(
                "transmission_upstream", [],
            ):
                if up and str(up).strip():
                    upstream_set.add(str(up).strip())

        industries: list[dict] = []
        stage_distribution: dict[str, int] = {}
        for cand in candidates:
            name = cand.get("name", "")
            view = stage_map.get(name) or {}
            stage = view.get("stage") or "导入期"
            if stage not in STAGE_DEFAULT_PRIORITY:
                stage = "导入期"
            imbalance = max(
                0.0, min(10.0, float(view.get("imbalance_score") or 0)),
            )
            stage_score = float(STAGE_DEFAULT_PRIORITY[stage])
            corrected = False
            # 供需强势修正: 阶段分非正但供需严重失衡 → 强制上调爆发前期,
            # 防止供需逻辑强劲的行业被静态阶段分拖累 (优化点 5 新形式)
            if stage_score <= 0 and imbalance >= IMBALANCE_STAGE_ESCALATION:
                stage = "爆发前期"
                stage_score = float(STAGE_DEFAULT_PRIORITY[stage])
                corrected = True
            # 上游传导加分: 该行业被其他候选行业点名为上游 (快人一步)
            upstream_bonus = BONUS_UPSTREAM if name in upstream_set else 0.0
            # 涨停潮右侧确认加分 (市场资金已完成基本面验证)
            in_wave = any(self._match_tag(name, w) for w in wave_names)
            wave_bonus = BONUS_WAVE if in_wave else 0.0
            cycle = view.get("expansion_cycle") or "中期"
            cycle_modifier = _CYCLE_MODIFIER.get(cycle, -0.5)
            barrier = view.get("entry_barrier") or "中"
            barrier_modifier = _BARRIER_MODIFIER.get(barrier, -0.5)
            # 各分项归一化到 0-1 区间后乘权重, 映射到 0-10 便于展示/落库
            raw_score = (
                (imbalance / 10.0) * WEIGHT_IMBALANCE
                + (stage_score / 5.0) * WEIGHT_STAGE
                + (upstream_bonus / 10.0) * WEIGHT_UPSTREAM
                + (wave_bonus / 10.0) * WEIGHT_WAVE
                + cycle_modifier * WEIGHT_CYCLE
                + barrier_modifier * WEIGHT_BARRIER
            )
            stage_distribution[stage] = stage_distribution.get(stage, 0) + 1
            industries.append({
                **cand,
                "stage": stage,
                "stage_corrected": corrected,
                "stage_analysis": view.get("analysis", ""),
                "imbalance_score": imbalance,
                "expansion_cycle": cycle,
                "entry_barrier": barrier,
                "transmission_upstream": list(
                    view.get("transmission_upstream") or [],
                ),
                "transmission_evidence": view.get("transmission_evidence", ""),
                "upstream_bonus": upstream_bonus,
                "wave_bonus": wave_bonus,
                "priority": round(max(0.0, min(10.0, raw_score * 10)), 2),
            })
        industries.sort(key=lambda i: i["priority"], reverse=True)
        top = industries[:MAX_INDUSTRIES_OUTPUT]
        # 行业榜数据源: 同一排序的前 INDUSTRY_BOARD_SIZE 个行业 (前端行业榜),
        # 与继续个股精选的 Top N 同源, 由 stock_selection 步落库 (§7.1)
        board = industries[:INDUSTRY_BOARD_SIZE]
        # 上游传导二次验证: Top 行业点名的上游未上排名榜时, 能验证 (候选池/
        # 板块库匹配) 则附加展示在行业榜 — 不强制入 Top, 不参与个股精选;
        # 无法验证的上游 (LLM 幻觉) 直接丢弃, 控制条数上限防止榜单膨胀
        transmission_rows = self._transmission_rows(top, industries, board, boards)
        if transmission_rows:
            board = sorted(
                board + transmission_rows,
                key=lambda i: i["priority"], reverse=True,
            )
            self.log(
                "transmission_board",
                reason=(
                    f"上游传导二次验证, 行业榜附加展示 {len(transmission_rows)} 个: "
                    f"{[(r['name'], r.get('transmission_from', '')) for r in transmission_rows]}"
                ),
                task_id=task_id, flow_id=flow_id,
            )

        # 优化点 7: 上报只携带关键结论 (丢弃涨跌家数等明细, 阶段分析截断)
        slim = [self._slim_industry(i) for i in top]
        result = {
            "decision": "ok",
            "trade_date": trade_date,
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
                f"综合排名 Top {len(top)}: "
                f"{[(i['name'], i['priority'], i['stage'], i['imbalance_score']) for i in top]}"
            ),
            task_id=task_id, flow_id=flow_id, detail=result,
        )
        self.report(flow_type, flow_id, step, result)
        return result

    @staticmethod
    def _slim_industry(ind: dict) -> dict:
        """Context 瘦身 (优化点 7): 丢弃当日涨跌明细字段, 阶段分析截断;
        保留供需关键字段供下游/前端展示."""
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
            # 供需第一性原理关键结论 (新增, 传导列表截断控制体积)
            "imbalance_score": ind.get("imbalance_score", 0),
            "expansion_cycle": ind.get("expansion_cycle", ""),
            "entry_barrier": ind.get("entry_barrier", ""),
            "transmission_upstream": (ind.get("transmission_upstream") or [])[:3],
            "upstream_bonus": ind.get("upstream_bonus", 0),
            "wave_bonus": ind.get("wave_bonus", 0),
            "transmission_from": ind.get("transmission_from", ""),
        }

    # -- 上游传导二次验证 (行业榜附加展示, 不入 Top) -------------------------

    def _transmission_rows(
        self,
        top: list[dict],
        industries: list[dict],
        board: list[dict],
        boards: list[dict],
    ) -> list[dict]:
        """上游传导二次验证: Top 行业点名的上游未上排名榜时, 能验证才附加展示 (压制
        LLM 幻觉): 1) 在候选池内 (已经 LLM 评估但排在榜外) → 复用其完整评估;
        2) 匹配全量板块库 → 按传导衰减保守计分建档; 3) 无法验证 → 丢弃."""
        rows: list[dict] = []

        def _listed(name: str) -> bool:
            pool = list(board) + rows
            return any(self._match_tag(b.get("name", ""), name) for b in pool)

        for ind in top:
            referrer = ind.get("name", "")
            for up in ind.get("transmission_upstream") or []:
                if len(rows) >= MAX_TRANSMISSION_BOARDS:
                    return rows
                up = str(up).strip()
                # 已上排名榜/已附加 (含指向自身) 不重复; 空名跳过
                if not up or _listed(up):
                    continue
                # 1) 候选池内 (可能排在榜外) → 直接擢升, 保留其完整评估字段
                cand = next(
                    (i for i in industries if self._match_tag(i.get("name", ""), up)),
                    None,
                )
                if cand is not None:
                    rows.append({**cand, "transmission_from": referrer})
                    continue
                # 2) 全量板块库匹配 → 传导衰减建档 (匹配不到即幻觉, 丢弃)
                master = next(
                    (b for b in boards
                     if b.get("name") and self._match_tag(b["name"], up)),
                    None,
                )
                if master is None:
                    logger.info(
                        "Transmission upstream unverifiable, dropped: %s (from %s)",
                        up, referrer,
                    )
                    continue
                rows.append(self._transmission_entry(master, ind))
        return rows

    @staticmethod
    def _transmission_entry(master: dict, referrer: dict) -> dict:
        """传导上游的行业榜建档 (未经 LLM 单独评估): 需求侧失衡分按指向行业衰减,
        阶段按导入期处理 (传导逻辑处早期), 同一加权公式保守计分."""
        imbalance = round(
            float(referrer.get("imbalance_score", 0) or 0) * TRANSMISSION_DECAY, 1,
        )
        evidence = (referrer.get("transmission_evidence") or "无明确证据")[:60]
        raw_score = (
            (imbalance / 10.0) * WEIGHT_IMBALANCE
            + (float(STAGE_DEFAULT_PRIORITY["导入期"]) / 5.0) * WEIGHT_STAGE
            + _CYCLE_MODIFIER["中期"] * WEIGHT_CYCLE
            + _BARRIER_MODIFIER["中"] * WEIGHT_BARRIER
        )
        return {
            "code": master.get("code", ""),
            "name": master.get("name", ""),
            "board_level": master.get("board_level", ""),
            "change_pct": float(master.get("change_pct", 0) or 0),
            "main_net_inflow": round(
                float(master.get("main_net_inflow", 0) or 0), 0,
            ),
            "event_tag": referrer.get("event_tag", ""),
            "stage": "导入期",
            "stage_corrected": False,
            "stage_analysis": (
                f"上游传导: {referrer.get('name', '')} 供需失衡向上游传导 ({evidence})"
            ),
            "imbalance_score": imbalance,
            "expansion_cycle": "",
            "entry_barrier": "",
            "transmission_upstream": [],
            "transmission_evidence": referrer.get("transmission_evidence", ""),
            "transmission_from": referrer.get("name", ""),
            "upstream_bonus": 0.0,
            "wave_bonus": 0.0,
            "priority": round(max(0.0, min(10.0, raw_score * 10)), 2),
        }

    # -- 候选行业池: 事件映射优先, 涨幅榜补足 ---------------------------------

    @staticmethod
    def _match_tag(board_name: str, tag: str) -> bool:
        """模糊匹配: 双向包含, 或后缀剥离后的核心词 (≥2字) 双向包含,
        如 新能源车→新能源; "机器人"/"减速器"等词不会被剥残 (见 _strip_industry_suffix)."""
        if not board_name or not tag:
            return False
        if tag in board_name or board_name in tag:
            return True
        core_board = _strip_industry_suffix(board_name)
        core_tag = _strip_industry_suffix(tag)
        return any(
            a in b or b in a
            for a, b in ((core_tag, board_name), (core_board, tag),
                         (core_tag, core_board))
        )

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
        event_type_map: dict[str, str] | None = None,
        trade_date: str = "",
    ) -> dict[str, dict]:
        """LLM 供需分析 + 生命周期定位 + 上游传导识别 (三位一体);
        只输出分项判断, 综合优先级由代码侧加权公式计算; 失败返回空."""
        lines = []
        for c in candidates:
            inflow_yi = c["main_net_inflow"] / 1e8
            in_wave = any(self._match_tag(c["name"], w) for w in wave_names)
            # 事件类型提示 (首次提出等): 仅文案提示, 不参与评分公式.
            et_label = _EVENT_TYPE_LABELS.get(
                (event_type_map or {}).get(c["event_tag"], ""),
            )
            lines.append(
                f"- {c['name']}({c['board_level']}): 当日涨幅 {c['change_pct']}%, "
                f"主力净流入 {inflow_yi:.2f} 亿, "
                f"上涨/下跌家数 {c['up_count']}/{c['down_count']}, "
                f"领涨股 {c['top_stock_name']}, "
                f"关联事件标签: {c['event_tag'] or '无'}"
                + (f" (事件类型: {et_label})" if et_label else "")
                + f", 今日有涨停潮: {'是' if in_wave else '否'}"
            )
        event_lines = "\n".join(
            f"- [{e.get('level', '')}] {e.get('title', '')}"
            + (
                f" ({_EVENT_TYPE_LABELS[e['event_type']]})"
                if e.get("event_type") in _EVENT_TYPE_LABELS else ""
            )
            + f" → {', '.join(e.get('impact_industries') or [])}"
            for e in events[:10]
        ) or "(无)"
        prompt = (
            "你是 A 股细分行业分析师, 擅长从'供需第一性原理'判断行业拐点, "
            "并具备产业链发散思维。请严格按以下框架顺序分析:\n"
            f"当前评估日期: {trade_date or '未知'}\n\n"
            "## 第一步: 供需分析 (核心维度)\n"
            "对每个候选行业依次回答:\n"
            "1. 需求驱动 (demand_driver): 近期事件是否带来需求爆发或收缩? "
            "具体是什么?\n"
            "2. 供给约束 (supply_constraint): 供给端是否有瓶颈? "
            "(如扩产谨慎/环保限产/技术壁垒)\n"
            "3. 供需失衡分 (imbalance_score, 0-10): 10=极度供不应求, "
            "0=极度供过于求\n"
            "4. 扩产周期 (expansion_cycle): 新增产能需要多久? "
            "短期(<6个月) / 中期(6-18个月) / 长期(>18个月)\n"
            "5. 进入壁垒 (entry_barrier): 极高 / 高 / 中 / 低\n\n"
            "## 第二步: 生命周期定位 (复用诊断矩阵)\n"
            f"{_LIFECYCLE_MATRIX}\n"
            "阶段取值必须为以下之一: "
            + " / ".join(STAGE_DEFAULT_PRIORITY) + "\n"
            "(处于验证期的行业请按业绩兑现进度归入 验证后期 或 消化期)\n"
            "特别规则: 若 imbalance_score ≥ 7 且扩产周期为长期, "
            "应优先定位为 爆发期 或 验证后期。\n"
            "特别规则 (新主题): 若行业因「首次提出」类全新题材/重大政策受关注 "
            "(见题面事件类型标记), 即便当前财报尚无业绩体现, 可给予"
            "「爆发前期」阶段与较高的供需预期分; 但必须在 analysis 中说明"
            "预期兑现的时间窗口 (如 12-18 个月)。后续评估中请结合当前评估日期"
            "判断距题材首次提出已过去多久: 若已跨过业绩披露期仍无营收/订单/"
            "毛利增长迹象, 阶段应主动下调至「消化期」或「衰退期」, "
            "不得因题材惯性维持高阶段。\n\n"
            "## 第三步: 上游传导发散\n"
            "若某行业明显供不应求, 思考其核心上游原材料/设备/零部件是否存在同样的"
            "供需紧张逻辑。示例: PCB 爆发 → 覆铜板/环氧树脂; 人形机器人产业化 → "
            "伺服电机、滚珠丝杠、谐波减速器、力矩传感器等核心零部件; "
            "半导体扩产 → 设备/材料。请优先挖掘此类高价值细分环节, "
            "并注明各自供需逻辑 (产能瓶颈、扩产周期等)。"
            "输出 transmission_upstream 列表与 transmission_evidence "
            "(涨价函/产能公告等)。被点名的上游行业在后续排名中将获得额外加分。\n"
            "重要: 传导证据必须来自题面事件与数据, 若无明确证据, "
            "请将 transmission_upstream 留空, 勿凭空编造; 上游名称尽量贴近 "
            "A 股板块常用叫法 (便于二次验证上榜)。\n\n"
            "## 第四步: 综合优先级\n"
            "无需输出综合分, 代码侧将按各分项加权计算, 只需输出分项判断。\n\n"
            f"## 当前重大宏观事件\n{event_lines}\n\n"
            f"## 候选行业当日数据\n" + "\n".join(lines) + "\n\n"
            "逐行业输出: name / demand_driver / supply_constraint / "
            "imbalance_score / expansion_cycle / entry_barrier / stage / "
            "analysis (综合判断依据, 150 字以内) / transmission_upstream / "
            "transmission_evidence。\n"
            "原则: 宁缺毋滥, 没有明显供需失衡的行业, imbalance_score 给 3 分以下。"
        )
        try:
            report = structured_invoke(
                self.llm.pick(deep=True), IndustryLifecycleList, prompt,
                default=IndustryLifecycleList(), deep=True,
            )
        except Exception as exc:
            logger.warning("Supply-lifecycle LLM report failed: %s", exc)
            return {}
        # priority 不在返回中: 综合分由代码侧加权公式计算, 不依赖 LLM
        return {
            item.name: {
                "stage": item.stage,
                "analysis": item.analysis,
                "imbalance_score": float(item.imbalance_score or 0),
                "expansion_cycle": item.expansion_cycle or "中期",
                "entry_barrier": item.entry_barrier or "中",
                "transmission_upstream": [
                    str(u).strip()
                    for u in (item.transmission_upstream or [])
                    if str(u).strip()
                ],
                "transmission_evidence": item.transmission_evidence or "",
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
        # 上游传导二次验证的附加行允许超出 INDUSTRY_BOARD_SIZE, 上限为附加条数配额
        board_slice = board_industries[: INDUSTRY_BOARD_SIZE + MAX_TRANSMISSION_BOARDS]
        for idx, ind in enumerate(board_slice, 1):
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
                "transmission_from": ind.get("transmission_from", ""),
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
    # 标签格式与 get_fundamentals (腾讯行情 + mootdx + 同花顺预期) 对齐.
    # 报告期 (东财 F10 REPORT_DATE) 放首位: 给 LLM 锚定财务数据新鲜度,
    # 防止把滞后旧财报误当最新业绩 (业绩验证阶段误判点);
    # 报告期模式多写法容错 (中英标签/冒号变体), 数据源格式漂移时不至于丢锚点.
    _FUND_METRIC_PATTERNS: tuple[tuple[str, str], ...] = (
        ("报告期", r"(?:Financial Report Period \(REPORT_DATE\)|REPORT_DATE|报告期)"
                   r"\s*[:：]\s*(\d{4}-\d{2}-\d{2})"),
        ("PE(TTM)", r"PE \(TTM\):\s*([-+]?\d+(?:\.\d+)?)"),
        ("PB", r"PB:\s*([-+]?\d+(?:\.\d+)?)"),
        ("ROE%", r"ROE \(%\):\s*([-+]?\d+(?:\.\d+)?)"),
        ("PEG", r"PEG:\s*([-+]?\d+(?:\.\d+)?)"),
        ("FwdPE", r"Forward PE \(FY(\d+)\):\s*([-+]?\d+(?:\.\d+)?)"),
        ("市值亿", r"Market Cap \(100M CNY\):\s*([-+]?\d+(?:\.\d+)?)"),
    )

    @classmethod
    def _extract_fund_metrics(cls, text: str) -> str:
        """基本面长文本 → 紧凑指标行 (省 Token, 空文本返回空).
        全部模式未命中时兜底返回原始片段摘要: 数据源格式漂移时, 宁给原文摘要,
        不让 LLM 彻底失去基本面上下文."""
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
        if parts:
            return ", ".join(parts)
        for line in text.splitlines():
            line = line.strip()
            # 跳过标题/注释/分隔行, 取首行实质内容截断后兜底返回
            if not line or line.startswith("#") or line.startswith("---"):
                continue
            return line[:80]
        return ""

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
