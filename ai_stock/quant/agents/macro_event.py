"""宏观事件解析 Agent (设计文档 §7.1.0, 队列: selection_control).

自上而下选股流程的第一步: 抓取近 3 日全市场热点新闻/政策快讯, 由
LLM 提取重大事件, 按影响力分级 (全球变革/国家级战略/行业级变化) 并
映射至直接利好的 A 股行业, 供下游行业生命周期定位使用.

原则:
- LLM 只做事件提取/分级/行业映射, 不做布尔型硬门槛过滤
- 事件→行业映射经本地关键词知识库预检交叉收敛, 压缩 LLM 幻觉 (优化点 2)
- 新闻为空或 LLM 不可用 → decision=empty 继续流程, 不阻断 Orchestrator,
  由下游 IndustryScanAgent 回退量化扫描 (优化点 6 降级链)
- 上报 Context 只携带行业名 + 事件摘要 (≤100 字), 不携带新闻原文 (优化点 7)
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from ..config import MACRO_NEWS_DAYS
from ..event_industry_kb import match_industries, reconcile
from ..llm_helper import structured_invoke
from .base import BaseAgent

logger = logging.getLogger(__name__)

# 单条新闻送入 Prompt 的最大条数与内容截断长度 (控制上下文成本)
_MAX_NEWS_IN_PROMPT = 30
_NEWS_CONTENT_CHARS = 120
# 上报 Context 的事件描述上限 (优化点 7: 只传结论不传原文)
_DESCRIPTION_CHARS = 100


class MacroEvent(BaseModel):
    """单个宏观事件 (LLM 结构化输出)."""

    title: str = ""
    level: Literal["global", "national", "industry"] = "industry"
    impact_industries: list[str] = Field(default_factory=list)
    description: str = ""
    influence_score: float = Field(default=5.0, ge=1, le=10)


class MacroEventReport(BaseModel):
    events: list[MacroEvent] = Field(default_factory=list)


class MacroEventAgent(BaseAgent):
    """宏观事件解析: 热点新闻 → LLM 提取事件 → 分级 + 利好行业映射."""

    agent_name = "macro_event"

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")

        news = self.data.get_hot_news(MACRO_NEWS_DAYS)
        if not news:
            result = {"decision": "empty", "events": [], "news_scanned": 0}
            self.log(
                "macro_event_empty", reason="近期热点新闻为空, 宁缺毋滥",
                task_id=task_id, flow_id=flow_id,
            )
            self.report(flow_type, flow_id, step, result)
            return result

        events, kb_hits = self._extract_events(news)
        result = {
            "decision": "ok" if events else "empty",
            "events": [self._slim_event(e) for e in events],
            "kb_industries": kb_hits,
            "news_scanned": len(news),
        }
        self.log(
            "macro_event_done" if events else "macro_event_empty",
            reason=(
                f"扫描 {len(news)} 条新闻, 提取重大事件 {len(events)} 个: "
                f"{[e.title for e in events[:5]]}"
            ),
            task_id=task_id, flow_id=flow_id, detail=result,
        )
        self.report(flow_type, flow_id, step, result)
        return result

    def _extract_events(
        self, news: list[dict],
    ) -> tuple[list[MacroEvent], list[str]]:
        """LLM 提取事件并按影响力降序; 失败/不可用时返回空 (不阻断流程).

        返回 (事件列表, 知识库预检命中行业列表)。行业映射经本地知识库
        交叉收敛 (优化点 2): 知识库命中的行业作为候选注入 Prompt,
        LLM 输出后再逐事件校验, 剔除无关键词依据的凭空关联。
        """
        if not self.llm.available:
            logger.warning("Macro event LLM unavailable; skip extraction")
            return [], []
        lines = []
        corpus_parts: list[str] = []
        for item in news[:_MAX_NEWS_IN_PROMPT]:
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            content = str(item.get("content", "") or "")[:_NEWS_CONTENT_CHARS]
            lines.append(
                f"- [{item.get('time', '')}] ({item.get('source', '')}) "
                f"{title}: {content}"
            )
            corpus_parts.append(f"{title} {content}")
        if not lines:
            return [], []

        # 关键词知识库预检: 先粗筛行业, 再将匹配结果(而非原始新闻)喂给 LLM 排序
        kb_hits = match_industries("\n".join(corpus_parts))
        kb_hint = (
            "本地知识库已预检出以下候选行业 (有关键词依据, 优先从中选取, "
            "也可补充确有依据的行业): "
            + ", ".join(kb_hits[:20]) + "\n"
        ) if kb_hits else ""
        prompt = (
            "你是宏观策略分析师。请阅读以下近几日 A 股市场热点新闻/政策快讯，"
            "识别对股市有重大影响的事件。\n"
            "按影响力分为三个层级:\n"
            "- global: 全球性变革 (如 AI 技术革命、地缘冲突、全球货币政策转折)\n"
            "- national: 国家级战略/政策 (如碳中和、新质生产力、大规模设备更新)\n"
            "- industry: 行业级变化 (如产业链价格战、关键技术突破、行业监管变化)\n"
            "对每个事件，映射其最直接利好的 A 股行业名称 "
            "(如: 碳中和→新能源/电力; AI 大模型→半导体/通信/计算机)。\n"
            "impact_industries 必须有新闻原文依据, 禁止凭联想输出无关行业。\n"
            f"{kb_hint}"
            "influence_score 按 1-10 评估事件的持续性、覆盖广度与市场影响力。\n"
            "只保留对 A 股有实质影响的事件，逐条输出 "
            "title/level/impact_industries/description/influence_score。\n\n"
            "新闻列表:\n" + "\n".join(lines)
        )
        try:
            report = structured_invoke(
                self.llm.pick(deep=True), MacroEventReport, prompt, deep=True,
            )
        except Exception as exc:
            logger.warning("Macro event extraction failed: %s", exc)
            return [], kb_hits
        events: list[MacroEvent] = []
        for e in report.events:
            if not e.title:
                continue
            # 逐事件知识库交叉收敛: 剔除无关键词依据的幻觉映射 (库无命中时保留兜底)
            verified = reconcile(
                list(e.impact_industries),
                match_industries(f"{e.title} {e.description}"),
            )
            if not verified:
                continue
            e.impact_industries = verified
            events.append(e)
        events.sort(key=lambda e: e.influence_score, reverse=True)
        return events, kb_hits

    @staticmethod
    def _slim_event(e: MacroEvent) -> dict:
        """上报 Context 瘦身: 只保留行业名与 ≤100 字摘要 (优化点 7)."""
        data = e.model_dump()
        data["description"] = (e.description or "")[:_DESCRIPTION_CHARS]
        return data
