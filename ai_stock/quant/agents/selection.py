"""选股 Agent 集群 (设计文档 §7.1, 队列: selection_control).

流程 (Orchestrator FSM 驱动):
    selection_start → 行业扫描 → 涨停潮监控 → 个股精选 → 深度分析 → 入自选池

原则:
- 基本面/资金面因子硬过滤为硬门槛 (§7.1.1), LLM 只生成景气度/投资逻辑
  报告与置信度, 不参与候选去留的布尔判断
- 宁缺毋滥: 任一环节无合格候选 → 空结果继续流程, 不强行凑数
- 北交所 (30% 涨跌幅) 与 ST/退市风险标的全程排除
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from .. import db_ops
from ..calendar_utils import get_trade_date
from ..config import MIN_ENTRY_CONFIDENCE
from ..llm_helper import structured_invoke
from ..news_store import get_news_store
from ..orchestrator import get_orchestrator
from .base import BaseAgent

logger = logging.getLogger(__name__)

# 控制成本: 最多分析的行业数 / 每行业入池股票数
MAX_INDUSTRIES = 4
STOCKS_PER_INDUSTRY = 2

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

class IndustryStageReport(BaseModel):
    """单个行业的景气度阶段判断 (LLM 只做解释, stage 供下游参考)."""

    name: str = ""
    stage: str = "initial"          # initial / confirmed / overheated
    analysis: str = ""


class IndustryScanReport(BaseModel):
    industries: list[IndustryStageReport] = Field(default_factory=list)


class DeepAnalysisReport(BaseModel):
    """个股深度分析报告 (§2.1 个股分析报告)."""

    reason: str = ""                # 入选理由
    bull_factors: list[str] = Field(default_factory=list)
    bear_factors: list[str] = Field(default_factory=list)
    stage_judgement: str = ""       # 当前所处阶段
    rise_trigger: str = ""          # 上涨触发条件预测
    confidence: float = 0.0         # 0-10, 低于门槛不入池
    risk_tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 7.1.1 行业扫描 Agent
# ---------------------------------------------------------------------------

class IndustryScanAgent(BaseAgent):
    """行业景气度扫描: 涨幅前5 ∪ 净流入前5 → 基本面因子硬过滤 → LLM 景气度报告."""

    agent_name = "industry_scan"

    # 硬过滤阈值 (§7.1.1: 基本面因子为硬门槛)
    MIN_INFLOW_YUAN = 0.0           # 净流入须为正 (资金初步证明)
    MAX_CHANGE_PCT = 8.0            # 当日涨幅过高 → 时机风险, 剔除
    MIN_CHANGE_PCT = 0.5            # 涨幅接近零 → 景气度未启动

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")
        boards = self.data.get_top_industries(5)

        candidates: list[dict] = []
        for board in boards:
            code = board.get("code", "")
            name = board.get("name", "")
            if not code or not name:
                continue
            change_pct = float(board.get("change_pct", 0) or 0)
            inflow = float(board.get("main_net_inflow", 0) or 0)
            # 硬过滤: 资金面 + 时机 (LLM 不参与布尔判断)
            if inflow <= self.MIN_INFLOW_YUAN and change_pct < 3.0:
                continue  # 既无资金认可也无动量
            if change_pct > self.MAX_CHANGE_PCT:
                continue  # 当日已大涨, 避免追高
            if change_pct < self.MIN_CHANGE_PCT:
                continue  # 景气度未启动
            candidates.append({
                "code": code,
                "name": name,
                "change_pct": change_pct,
                "main_net_inflow": round(inflow, 0),
                "up_count": board.get("up_count", 0),
                "down_count": board.get("down_count", 0),
                "top_stock_name": board.get("top_stock_name", ""),
                "top_stock_code": board.get("top_stock_code", ""),
                "board_level": board.get("board_level", ""),
            })

        candidates = candidates[:MAX_INDUSTRIES]
        report_map = self._llm_stage_report(candidates)

        industries = []
        for cand in candidates:
            llm_view = report_map.get(cand["name"], {})
            industries.append({
                **cand,
                "stage": llm_view.get("stage", "initial"),
                "stage_analysis": llm_view.get("analysis", ""),
            })

        result = {
            "decision": "ok" if industries else "empty",
            "trade_date": context.get("trade_date", get_trade_date()),
            "industries": industries,
            "scanned": len(boards),
            "filtered_in": len(industries),
        }
        self.log(
            "scan_done" if industries else "scan_empty",
            reason=f"扫描 {len(boards)} 板块, 硬过滤后 {len(industries)} 个候选行业",
            task_id=task_id, flow_id=flow_id, detail=result,
        )
        self.report(flow_type, flow_id, step, result)
        return result

    def _llm_stage_report(self, candidates: list[dict]) -> dict[str, dict]:
        """LLM 生成景气度阶段报告 (仅解释; 失败降级为 initial)."""
        if not candidates or not self.llm.available:
            return {}
        lines = []
        for c in candidates:
            inflow_yi = c["main_net_inflow"] / 1e8
            lines.append(
                f"- {c['name']}({c['board_level']}): 当日涨幅 {c['change_pct']}%, "
                f"主力净流入 {inflow_yi:.2f} 亿, 上涨/下跌家数 {c['up_count']}/{c['down_count']}, "
                f"领涨股 {c['top_stock_name']}"
            )
        prompt = (
            "以下是 A 股今日候选行业板块数据。请对每个行业判断景气度所处阶段:\n"
            "- initial: 景气度初始阶段 (供需关系刚开始改变, 尚未被充分定价)\n"
            "- confirmed: 景气度已被初步证明 (资金持续流入 + 领涨股确立)\n"
            "- overheated: 已大幅上涨/过热 (追高风险大)\n"
            "判断依据优先考虑供需关系改变、国家政策、社会发展趋势 (如人形机器人);\n"
            "注意避免已大幅上涨的行业。逐行业输出 name/stage/analysis。\n\n"
            + "\n".join(lines)
        )
        try:
            report = structured_invoke(
                self.llm.pick(), IndustryScanReport, prompt,
                default=IndustryScanReport(), deep=True,
            )
        except Exception as exc:
            logger.warning("Industry stage LLM report failed: %s", exc)
            return {}
        return {
            item.name: {"stage": item.stage, "analysis": item.analysis}
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
            waves.append({
                "name": theme,
                "limit_up_count": len(stocks),
                "avg_change_pct": round(sum(changes) / len(changes), 2) if changes else 0.0,
                "samples": [
                    {"code": s.get("code", ""), "name": s.get("name", "")}
                    for s in stocks[:6]
                ],
                "risk_tags": risk_tags,
            })

        # 涨停家数降序, 截断
        waves.sort(key=lambda w: w["limit_up_count"], reverse=True)
        waves = waves[:MAX_INDUSTRIES]

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
            task_id=task_id, flow_id=flow_id, detail=result,
        )
        self.report(flow_type, flow_id, step, result)
        return result


# ---------------------------------------------------------------------------
# 7.1.3 个股精选 Agent
# ---------------------------------------------------------------------------

class StockSelectionAgent(BaseAgent):
    """候选行业内四维打分精选: 每行业选 2 只最优标的 (关联/市占/壁垒/弹性)."""

    agent_name = "stock_selection"

    # 四维权重 (市场风格自适应, §7.1.3)
    WEIGHTS = {
        "aggressive": {"relevance": 0.25, "dominance": 0.15, "moat": 0.20, "elasticity": 0.40},
        "neutral": {"relevance": 0.30, "dominance": 0.25, "moat": 0.25, "elasticity": 0.20},
        "defensive": {"relevance": 0.25, "dominance": 0.35, "moat": 0.30, "elasticity": 0.10},
    }

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")

        industry_result = context.get("industry_scan") or {}
        wave_result = context.get("limit_up_monitor") or {}
        industries = list(industry_result.get("industries") or [])
        waves = list(wave_result.get("waves") or [])

        # 合并候选行业 (东财板块 + 涨停潮题材; 名称相同的合并)
        merged: list[dict] = []
        seen_names = set()
        for board in industries:
            merged.append({**board, "source": "board"})
            seen_names.add(board.get("name", ""))
        for wave in waves:
            if wave.get("name") in seen_names:
                continue  # 已有东财板块数据, 跳过同名题材
            merged.append({**wave, "source": "wave"})
            seen_names.add(wave.get("name", ""))
        merged = merged[:MAX_INDUSTRIES]

        if not merged:
            result = {"decision": "empty", "selected": [], "style": "neutral"}
            self.log(
                "select_empty", reason="无候选行业, 宁缺毋滥",
                task_id=task_id, flow_id=flow_id,
            )
            self.report(flow_type, flow_id, step, result)
            return result

        style = self._market_style(merged)
        weights = self.WEIGHTS[style]
        selected: list[dict] = []

        for industry in merged:
            candidates = self._industry_candidates(industry)
            if not candidates:
                continue
            for cand in candidates:
                dims = self._four_dimensions(cand, industry)
                cand["dimensions"] = dims
                cand["score"] = round(
                    sum(dims[k] * weights[k] for k in weights), 2,
                )
                cand["risk_tags"] = self._risk_tags(cand)
            candidates.sort(key=lambda c: c["score"], reverse=True)
            for cand in candidates[:STOCKS_PER_INDUSTRY]:
                selected.append({
                    "symbol": cand["code"],
                    "name": cand["name"],
                    "industry": industry.get("name", ""),
                    "industry_source": industry.get("source", ""),
                    "score": cand["score"],
                    "dimensions": cand["dimensions"],
                    "risk_tags": cand["risk_tags"],
                })

        result = {
            "decision": "ok" if selected else "empty",
            "style": style,
            "selected": selected,
        }
        self.log(
            "select_done" if selected else "select_empty",
            reason=(
                f"风格={style}, {len(merged)} 个行业选出 {len(selected)} 只: "
                f"{[s['symbol'] for s in selected]}"
            ),
            task_id=task_id, flow_id=flow_id, detail=result,
        )
        self.report(flow_type, flow_id, step, result)
        return result

    # -- 行业内候选获取 ------------------------------------------------------

    def _industry_candidates(self, industry: dict) -> list[dict]:
        """行业候选股: 东财板块 → 龙头列表; 涨停潮题材 → 涨停样本股."""
        candidates: list[dict] = []
        if industry.get("source") == "board" and industry.get("code"):
            leaders = self.data.get_board_leaders(industry["code"], top_n=6)
            for leader in leaders:
                if _is_excluded(leader.get("code", ""), leader.get("name", "")):
                    continue
                candidates.append(dict(leader))
        else:
            # 涨停潮: 样本股即板块代表, 补充实时行情数据
            for sample in industry.get("samples", []):
                code = sample.get("code", "")
                if not code or _is_excluded(code, sample.get("name", "")):
                    continue
                quote = self.data.get_realtime_quote(code)
                if not quote:
                    continue
                candidates.append({
                    "code": code,
                    "name": sample.get("name") or quote.get("name", ""),
                    "change_pct": quote.get("change_pct", 0.0),
                    "turnover_rate": 0.0,
                    "volume_ratio": 0.0,
                    "market_cap": quote.get("market_cap", 0.0),
                    "main_net_inflow": 0.0,
                    "leader_label": "涨停样本",
                })
        return candidates

    # -- 四维指标 (数据可得性代理, §7.1.3) -----------------------------------

    @staticmethod
    def _four_dimensions(cand: dict, industry: dict) -> dict:
        """四维打分 0-10: 关联性/市占率/技术壁垒/弹性 (结构化数据代理)."""
        # 关联性: 领涨标签/涨停样本 → 与板块主题关联最强
        label = cand.get("leader_label", "")
        relevance = 10.0 if label in ("领涨", "涨停样本") else 8.0
        inflow = float(cand.get("main_net_inflow", 0) or 0)
        if inflow > 0:
            relevance = min(relevance + 1.0, 10.0)  # 主力资金主攻 = 强关联

        # 市占率: 市值代理 (龙头大市值 ≈ 高市占)
        mcap = float(cand.get("market_cap", 0) or 0)
        if mcap <= 0:
            dominance = 5.0
        else:
            mcap_yi = mcap / 1e8
            dominance = min(10.0, max(3.0, 2.0 + mcap_yi ** 0.25 * 1.6))

        # 技术壁垒: 大市值 + 主力净流入 (龙头地位与资金共识代理)
        moat = dominance * 0.6
        if inflow > 0:
            moat += min(inflow / 1e8, 10.0) * 0.4
        moat = min(round(moat, 2), 10.0)

        # 弹性: 换手率 + 量比 + 中小市值加分
        turnover = min(float(cand.get("turnover_rate", 0) or 0), 30.0)
        volume_ratio = min(float(cand.get("volume_ratio", 0) or 0), 5.0)
        elasticity = turnover / 30.0 * 5.0 + volume_ratio / 5.0 * 3.0
        if 0 < mcap < 3e10:  # 300 亿以下中小盘弹性更高
            elasticity += 2.0
        elasticity = min(round(elasticity, 2), 10.0)

        return {
            "relevance": round(relevance, 2),
            "dominance": round(dominance, 2),
            "moat": round(moat, 2),
            "elasticity": elasticity,
        }

    @staticmethod
    def _market_style(industries: list[dict]) -> str:
        """市场风格判断: 候选板块平均涨幅 → aggressive / neutral / defensive."""
        changes = [float(i.get("change_pct", 0) or 0) for i in industries]
        if not changes:
            return "neutral"
        avg = sum(changes) / len(changes)
        if avg > 2.0:
            return "aggressive"   # 进攻: 弹性权重放大
        if avg < -1.0:
            return "defensive"    # 防守: 市占/壁垒权重放大
        return "neutral"

    @staticmethod
    def _risk_tags(cand: dict) -> list[str]:
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
# 7.1.4 深度分析 Agent
# ---------------------------------------------------------------------------

class DeepAnalysisAgent(BaseAgent):
    """入选个股深度分析: LLM 生成投资逻辑报告 → 置信度门槛 → 写入自选池."""

    agent_name = "deep_analysis"

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")
        selected = (context.get("stock_selection") or {}).get("selected") or []

        entries: list[dict] = []
        skipped: list[dict] = []
        for stock in selected:
            symbol = stock.get("symbol", "")
            if not symbol:
                continue
            entry = self._analyze_one(symbol, stock, flow_id)
            if entry is None:
                skipped.append({
                    "symbol": symbol,
                    "reason": "LLM 不可用或数据缺失, 宁缺毋滥不入池",
                })
                continue
            if entry["confidence"] < MIN_ENTRY_CONFIDENCE:
                skipped.append({
                    "symbol": symbol,
                    "reason": f"置信度 {entry['confidence']} 低于门槛 {MIN_ENTRY_CONFIDENCE}",
                })
                self.log(
                    "skip_low_confidence", symbol=symbol,
                    reason=f"置信度 {entry['confidence']} 不足",
                    task_id=task_id, flow_id=flow_id,
                )
                continue
            db_ops.upsert_optional_stock(entry)
            entries.append(entry)
            self.log(
                "pool_added", symbol=symbol,
                reason=f"入池: {entry['reason'][:200]}",
                task_id=task_id, flow_id=flow_id,
                detail={
                    "confidence": entry["confidence"],
                    "bear_factors": entry["bear_factors"],
                    "risk_tags": entry["risk_tags"],
                },
            )

        result = {
            "decision": "ok" if entries else "empty",
            "pool_entries": [
                {"symbol": e["symbol"], "confidence": e["confidence"]} for e in entries
            ],
            "skipped": skipped,
        }
        self.log(
            "deep_analysis_done",
            reason=f"深度分析 {len(selected)} 只, 入池 {len(entries)} 只, 跳过 {len(skipped)} 只",
            task_id=task_id, flow_id=flow_id, detail=result,
        )
        self.report(flow_type, flow_id, step, result)
        return result

    def _analyze_one(self, symbol: str, stock: dict, flow_id: str) -> Optional[dict]:
        """单只股票深度分析 (数据收集 + 新闻入库 + LLM 报告)."""
        try:
            news = self.data.get_stock_news(symbol, hours=72)
            fundamentals = self.data.get_fundamentals_text(symbol)
            forecast = self.data.get_profit_forecast_text(symbol)
            lockup = self.data.get_lockup_expiry_text(symbol)
            insider = self.data.get_insider_text(symbol)
            concepts = self.data.get_concept_blocks(symbol)
        except Exception as exc:
            logger.warning("Deep analysis data fetch failed for %s: %s", symbol, exc)
            return None

        # 新闻入向量库 (供崩塌检测/清仓决策检索)
        try:
            get_news_store().add_news(news, symbol=symbol)
        except Exception as exc:
            logger.warning("news store add failed for %s: %s", symbol, exc)

        news_digest = "\n".join(
            f"- [{n.get('time', '')}] {n.get('title', '')}: {(n.get('content', '') or '')[:120]}"
            for n in news[:10]
        )
        prompt = (
            f"对 A 股 {symbol} {stock.get('name', '')} 生成投资逻辑深度分析报告。\n\n"
            f"所属行业: {stock.get('industry', '')} (来源: {stock.get('industry_source', '')}, "
            f"精选得分 {stock.get('score', 0)}, 风险标签 {stock.get('risk_tags', [])})\n\n"
            f"## 近期新闻\n{news_digest or '(无)'}\n\n"
            f"## 基本面数据\n{(fundamentals or '(无)')[:1500]}\n\n"
            f"## 一致预期\n{(forecast or '(无)')[:500]}\n\n"
            f"## 解禁日程\n{(lockup or '(无)')[:300]}\n\n"
            f"## 高管增减持\n{(insider or '(无)')[:300]}\n\n"
            f"## 所属概念板块\n{', '.join(concepts[:15]) or '(无)'}\n\n"
            "请输出 JSON: reason(入选理由), bull_factors(主要利多列表, 尽量具体), "
            "bear_factors(主要利空列表, 重点输出), stage_judgement(当前所处阶段, 如"
            "行业爆发前夕/大盘结构性调整/技术回调), rise_trigger(什么情况下会出现上涨), "
            "confidence(0-10 综合置信度, 宁缺毋滥请从严), risk_tags(风险标签列表)."
        )
        try:
            report = structured_invoke(
                self.llm.pick(deep=True), DeepAnalysisReport, prompt,
                deep=True,
            )
        except Exception as exc:
            logger.warning("Deep analysis LLM failed for %s: %s", symbol, exc)
            return None

        return {
            "symbol": symbol,
            "name": stock.get("name", ""),
            "industry": stock.get("industry", ""),
            "reason": report.reason,
            "bull_factors": report.bull_factors,
            "bear_factors": report.bear_factors,
            "stage_judgement": report.stage_judgement,
            "rise_trigger": report.rise_trigger,
            "risk_tags": report.risk_tags or stock.get("risk_tags", []),
            "confidence": round(float(report.confidence or 0.0), 1),
            "report": f"入选理由: {report.reason}\n利多: {'; '.join(report.bull_factors)}\n"
                      f"利空: {'; '.join(report.bear_factors)}\n"
                      f"阶段: {report.stage_judgement}\n上涨触发: {report.rise_trigger}",
        }


# ---------------------------------------------------------------------------
# selection_start: 调度入口 → 启动 FSM 流程
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
