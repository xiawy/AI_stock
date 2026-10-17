"""Ops monitoring: alert levels + silence window + daily report (设计文档 §14).

- 告警分级: WARNING(非阻断) / ERROR(任务失败, flow 超时) / CRITICAL(Redis
  异常, 死信堆积, 全局风控冻结)
- 告警降噪: 同类告警 5 分钟静默窗口, 窗口内重复事件仅记录一次
- 每日运行报告: 选股数、成交、规则版本变更、全局开关状态、异常统计
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import db_ops
from .config import ALERT_SILENCE_SECONDS

logger = logging.getLogger(__name__)

WARNING = "WARNING"
ERROR = "ERROR"
CRITICAL = "CRITICAL"

_LEVEL_LOG = {WARNING: logger.warning, ERROR: logger.error, CRITICAL: logger.critical}


class AlertManager:
    """分级告警 + 静默窗口 (告警风暴抑制) + 外部通道 (A3)."""

    def __init__(self, silence_seconds: int = ALERT_SILENCE_SECONDS):
        self.silence_seconds = silence_seconds
        self._last_emit: dict[str, float] = {}

    def emit(self, level: str, alert_type: str, message: str) -> bool:
        """发出告警; 静默窗口内同类告警仅推送一次. 返回是否真正发出."""
        import time

        now = time.monotonic()
        last = self._last_emit.get(alert_type, 0.0)
        if now - last < self.silence_seconds:
            logger.debug("Alert %s silenced (window %.0fs)", alert_type, self.silence_seconds)
            return False
        self._last_emit[alert_type] = now
        # 长期运行防泄漏: 超出上限时淘汰最旧的告警类型条目 (过期条目无复用语义)
        if len(self._last_emit) > 500:
            for _ in range(len(self._last_emit) - 500):
                self._last_emit.pop(next(iter(self._last_emit)), None)
        log_fn = _LEVEL_LOG.get(level, logger.warning)
        log_fn("[%s] %s: %s", level, alert_type, message)
        if level in (ERROR, CRITICAL):
            db_ops.log_decision(
                agent="ops_monitor",
                decision=f"alert_{level.lower()}",
                reason=message,
                detail={"alert_type": alert_type, "level": level},
            )
        # 外部通道: 只落库等于没有告警 —— 历史上「连续 6 周 0 成交」「行业扫描
        # 94% 空转」都属于「严重但不报错」的静默故障, 落库后无人查看。
        self._push_external(level, alert_type, message)
        return True

    @staticmethod
    def _push_external(level: str, alert_type: str, message: str) -> None:
        """推送外部通道 (企业微信机器人 / 通用 webhook). 未配置则静默跳过."""
        from .config import ALERT_WEBHOOK_TIMEOUT, ALERT_WEBHOOK_URL

        if not ALERT_WEBHOOK_URL:
            return
        text = f"[{level}] {alert_type}: {message}"
        try:
            import requests

            resp = requests.post(
                ALERT_WEBHOOK_URL,
                json={"msgtype": "text", "text": {"content": text}},
                timeout=ALERT_WEBHOOK_TIMEOUT,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "alert webhook rejected (%s): %s", resp.status_code, resp.text[:200],
                )
        except Exception as exc:
            # 外部通道故障绝不影响主业务
            logger.warning("alert webhook failed: %s", exc)


_alerts: Optional[AlertManager] = None


def get_alerts() -> AlertManager:
    global _alerts
    if _alerts is None:
        _alerts = AlertManager()
    return _alerts


def check_business_slo() -> dict:
    """业务级 SLO 巡检 (A3): 捕捉「严重但不报错」的静默故障.

    三个历史事故的对应判据:
    - ``industry_scan`` 连续 N 次空 → 选股上游断流 (本次事故: 94% 空转 6 周)
    - 连续 N 个交易日无成交 → 交易链路整体失效 (本次事故: 0 成交 6 周)
    - 选股降级占比过高 → 长期靠「续持旧池」, 池子必然老化
    - 数据源成功率跌破阈值 → 供应商被反爬封禁 (本次事故的根因)

    返回 ``{issues: [...], alerts: n}``; 每次巡检都会经 AlertManager 静默窗口去重。
    """
    from .config import (
        SLO_NO_TRADE_DAYS,
        SLO_SCAN_EMPTY_STREAK,
        SLO_SELECT_FALLBACK_RATIO,
    )

    issues: list[str] = []
    alerts = get_alerts()

    # 1) 行业扫描连续空转
    try:
        recent_scan = db_ops.get_decisions(
            limit=int(SLO_SCAN_EMPTY_STREAK), agent="industry_scan",
        )
        if (
            len(recent_scan) >= SLO_SCAN_EMPTY_STREAK
            and all(d.get("decision") == "scan_empty" for d in recent_scan)
        ):
            issues.append("industry_scan_empty_streak")
            alerts.emit(
                WARNING, "slo_industry_scan_empty",
                f"行业扫描连续 {SLO_SCAN_EMPTY_STREAK} 次无候选行业 — "
                "选股上游疑似断流 (检查板块数据源健康度)",
            )
    except Exception as exc:
        logger.debug("SLO industry_scan check failed: %s", exc)

    # 2) 选股降级占比
    try:
        selection = db_ops.get_decisions(limit=200, agent="stock_selection")
        total = len(selection)
        fallback = sum(1 for d in selection if d.get("decision") == "select_fallback")
        if total >= 20:
            ratio = fallback / total
            if ratio > SLO_SELECT_FALLBACK_RATIO:
                issues.append(f"select_fallback_ratio={ratio:.0%}")
                alerts.emit(
                    WARNING, "slo_select_fallback",
                    f"选股降级占比 {ratio:.0%} (阈值 {SLO_SELECT_FALLBACK_RATIO:.0%}), "
                    "长期靠续持旧池, 自选池将老化",
                )
    except Exception as exc:
        logger.debug("SLO select fallback check failed: %s", exc)

    # 3) 连续 N 个交易日无成交
    try:
        from .calendar_utils import count_trading_days

        trades = db_ops.get_trades(limit=1)
        if trades:
            last_time = trades[0].get("trade_time") or ""
            last_day = datetime.fromisoformat(last_time).date() if last_time else None
        else:
            last_day = None
        if last_day is None:
            issues.append("no_trade_ever")
            alerts.emit(
                CRITICAL, "slo_no_trade",
                "量化账户从未产生任何成交 — 请检查 global_trade_enable / 数据源 / 选股漏斗",
            )
        else:
            idle = count_trading_days(last_day, datetime.now().date())
            if idle > SLO_NO_TRADE_DAYS:
                issues.append(f"no_trade_days={idle}")
                alerts.emit(
                    CRITICAL, "slo_no_trade",
                    f"已连续 {idle} 个交易日无成交 (阈值 {SLO_NO_TRADE_DAYS}) — "
                    "交易链路整体失效",
                )
    except Exception as exc:
        logger.debug("SLO no-trade check failed: %s", exc)

    # 4) 数据源健康度
    try:
        from ai_stock.dataflows.data_health import degraded_sources

        from .config import DATA_HEALTH_SUCCESS_RATE_ALERT

        for row in degraded_sources(threshold=DATA_HEALTH_SUCCESS_RATE_ALERT):
            issues.append(f"source_degraded:{row['source']}/{row['api']}")
            alerts.emit(
                WARNING, f"slo_source_{row['source']}",
                f"数据源 {row['source']}/{row['api']} 成功率 "
                f"{row['success_rate']:.0%} (最近错误: {row['last_error'][:120]})",
            )
    except Exception as exc:
        logger.debug("SLO data health check failed: %s", exc)

    return {"issues": issues, "alerts": len(issues)}


def report_dir() -> Path:
    root = Path(__file__).resolve().parent.parent.parent
    out = root / "backend" / "data" / "quant_reports"
    out.mkdir(parents=True, exist_ok=True)
    return out


def generate_daily_report(trade_date: Optional[str] = None) -> dict:
    """生成每日运行报告 (§14.4), 写入 backend/data/quant_reports/."""
    from .mq import get_mq_backend
    from .config import ALL_QUEUES

    date_str = trade_date or datetime.now().strftime("%Y-%m-%d")
    backend = get_mq_backend()
    stats = backend.stats(list(ALL_QUEUES))

    optional_active = db_ops.get_optional_pool("active")
    # 查询下推: decision + 日期过滤在 SQL 完成, 不再拉 500 条后 Python 端过滤
    optional_removed = db_ops.get_decisions(
        limit=500, agent="logic_guard", decision="remove", date=date_str,
    )
    trades = db_ops.get_trades(date_str=date_str, limit=0)  # 不截断, 防超 200 笔统计失真
    holdings = db_ops.get_holdings()
    today_decisions = db_ops.get_decisions(limit=500, date=date_str)
    errors = [d for d in today_decisions if "error" in (d.get("decision") or "")]
    alerts = [
        d for d in today_decisions
        if d.get("agent") == "ops_monitor"
    ]

    flows_running = db_ops.get_running_flows()
    dead_total = sum(stats.get("dead", {}).values())

    # T7: 基准与归因 —— 没有「超额收益 / 胜率盈亏比 / 行业归因」这三个数,
    # 后续所有参数调优都是盲调。
    performance = _performance_stats(trades, holdings)
    attribution = _industry_attribution(trades)
    benchmark = _benchmark_snapshot()
    account_return = _account_daily_return()
    excess = None
    if benchmark.get("change_pct") is not None and account_return is not None:
        excess = round(account_return - benchmark["change_pct"], 4)

    report = {
        "date": date_str,
        "generated_at": datetime.now().isoformat(),
        "global_trade_enable": db_ops.get_config_value("global_trade_enable", "1"),
        "trade_frozen": db_ops.get_config_value("trade_frozen", "0"),
        "selection": {
            "optional_pool_active": len(optional_active),
            "logic_removed_today": len(optional_removed),
        },
        "trades": {
            "count": len(trades),
            "buy": sum(1 for t in trades if t["side"] == "buy"),
            "sell": sum(1 for t in trades if t["side"] == "sell"),
            "total_amount": round(sum(t["amount"] for t in trades), 2),
            "total_fee": round(sum(t["fee"] for t in trades), 2),
        },
        "holdings": {
            "count": len(holdings),
            "symbols": [h["symbol"] for h in holdings],
        },
        "performance": performance,
        "attribution_by_industry": attribution,
        "benchmark": benchmark,
        "account_return_pct": account_return,
        "excess_return_pct": excess,
        "data_sources": _data_source_health(),
        "queues": stats,
        "dead_letter_total": dead_total,
        "flows_running": len(flows_running),
        "decisions_today": len(today_decisions),
        "errors_today": len(errors),
        "alerts_today": len(alerts),
    }

    # markdown 导出
    try:
        lines = [
            f"# 量化交易日报 {date_str}",
            "",
            f"- 生成时间: {report['generated_at']}",
            f"- 全局交易开关: {report['global_trade_enable']}"
            f"{' (开启)' if report['global_trade_enable'] == '1' else ' (关闭)'}",
            f"- 账户风控冻结: {report['trade_frozen']}",
            "",
            "## 自选池",
            f"- 活跃标的: {report['selection']['optional_pool_active']} 只",
            f"- 今日逻辑崩塌移除: {report['selection']['logic_removed_today']} 只",
            "",
            "## 交易",
            f"- 当日委托 {report['trades']['count']} 笔"
            f" (买 {report['trades']['buy']} / 卖 {report['trades']['sell']})",
            f"- 成交金额: ¥{report['trades']['total_amount']:,} | 费用: ¥{report['trades']['total_fee']:,}",
            "",
            "## 绩效与归因",
        ]
        perf = report["performance"]
        lines += [
            f"- 胜率: {_pct(perf.get('win_rate'))} "
            f"(平仓 {perf.get('closed_trades', 0)} 笔)",
            f"- 盈亏比: {_num(perf.get('profit_factor'))} | "
            f"平均盈利 ¥{perf.get('avg_win', 0):,.0f} | 平均亏损 ¥{perf.get('avg_loss', 0):,.0f}",
            f"- 平均持仓天数 (在仓): {_num(perf.get('avg_holding_days_open'))}",
            f"- 当日账户收益: {_pct(report.get('account_return_pct'))} | "
            f"沪深300: {_pct(report['benchmark'].get('change_pct'))} | "
            f"超额: {_pct(report.get('excess_return_pct'))}",
        ]
        for row in report["attribution_by_industry"]:
            lines.append(
                f"  - {row['industry']}: ¥{row['realized_pnl']:,.0f} "
                f"({row['trades']} 笔)"
            )
        if report["benchmark"].get("error"):
            lines.append(f"  - 基准取数失败: {report['benchmark']['error']}")
        lines += ["", "## 持仓"]
        for h in holdings:
            lines.append(
                f"- {h['symbol']} {h['name']} × {h['quantity']} @ {h['cost_price']}"
                f" (止损 {h['stop_loss']})",
            )
        lines += [
            "",
            "## 系统状态",
            f"- 队列后端: {stats.get('backend')}",
            f"- 死信总数: {dead_total}",
            f"- 运行中流程: {len(flows_running)}",
            f"- 今日决策日志: {report['decisions_today']} 条 (异常 {report['errors_today']})",
        ]
        for src in report["data_sources"]:
            lines.append(
                f"- 数据源 {src['source']}/{src['api']}: 成功率 "
                f"{_pct(src.get('success_rate'))}, P95 {src.get('p95_latency_ms')}ms"
                + (f", 退避 {src['backoff_seconds']}s" if src.get("backoff_seconds") else "")
            )
        path = report_dir() / f"quant_report_{date_str}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        report["report_file"] = str(path)
        logger.info("Daily quant report written: %s", path)
    except Exception as exc:
        logger.warning("Daily report export failed: %s", exc)

    return report


# ---------------------------------------------------------------------------
# 日报辅助: 绩效 / 归因 / 基准 (T7)
# ---------------------------------------------------------------------------

def _pct(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:+.2%}"


def _num(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.2f}"


def _performance_stats(trades: list[dict], holdings: list[dict]) -> dict:
    """胜率 / 盈亏比 / 平均持仓天数.

    平仓口径: ``side='sell'`` 且 ``realized_pnl`` 非 0 的成交 (卖出时 broker
    已按参考成本算好该笔盈亏)。持仓天数取当前在仓标的的 buy_time。
    """
    closed = [
        t for t in trades
        if t.get("side") == "sell" and float(t.get("realized_pnl") or 0) != 0
    ]
    wins = [float(t["realized_pnl"]) for t in closed if float(t["realized_pnl"]) > 0]
    losses = [float(t["realized_pnl"]) for t in closed if float(t["realized_pnl"]) < 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    profit_factor = (
        round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None
    )

    days: list[int] = []
    now = datetime.now()
    for h in holdings:
        buy_time = h.get("buy_time") or ""
        try:
            days.append((now - datetime.fromisoformat(buy_time)).days)
        except (ValueError, TypeError):
            continue
    return {
        "closed_trades": len(closed),
        "win_rate": round(len(wins) / len(closed), 4) if closed else None,
        "profit_factor": profit_factor,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "realized_pnl": round(sum(float(t.get("realized_pnl") or 0) for t in closed), 2),
        "avg_holding_days_open": round(sum(days) / len(days), 2) if days else None,
    }


def _industry_attribution(trades: list[dict]) -> list[dict]:
    """按行业拆解已实现盈亏 (行业归口来自持仓记录/自选池)."""
    buckets: dict[str, dict] = {}
    for t in trades:
        if t.get("side") != "sell":
            continue
        symbol = t.get("symbol", "")
        industry = "未分类"
        try:
            from .risk_control import _resolve_industry

            industry = _resolve_industry(symbol, db_ops.get_holding(symbol)) or "未分类"
        except Exception:
            pass
        bucket = buckets.setdefault(industry, {"industry": industry, "realized_pnl": 0.0, "trades": 0})
        bucket["realized_pnl"] += float(t.get("realized_pnl") or 0)
        bucket["trades"] += 1
    rows = sorted(buckets.values(), key=lambda r: abs(r["realized_pnl"]), reverse=True)
    for row in rows:
        row["realized_pnl"] = round(row["realized_pnl"], 2)
    return rows


def _benchmark_snapshot() -> dict:
    """沪深300 当日涨跌幅 (基准). 取数失败返回 error, 不影响日报生成."""
    try:
        from ai_stock.dataflows.a_stock import _sina_kline_fallback

        from .config import MARKET_INDEX_CODE, MARKET_INDEX_PREFIX

        df = _sina_kline_fallback(
            MARKET_INDEX_CODE, prefix=MARKET_INDEX_PREFIX,
        )
        if df is None or len(df) < 2:
            return {"code": MARKET_INDEX_CODE, "change_pct": None, "error": "指数数据不足"}
        last = float(df["Close"].iloc[-1])
        prev = float(df["Close"].iloc[-2])
        return {
            "code": MARKET_INDEX_CODE,
            "close": round(last, 2),
            "change_pct": round(last / prev - 1.0, 6) if prev else None,
            "error": "",
        }
    except Exception as exc:
        logger.warning("benchmark snapshot failed: %s", exc)
        return {"code": "000300", "change_pct": None, "error": str(exc)[:120]}


def _account_daily_return() -> Optional[float]:
    """账户当日收益率 (基于总资产与当日已实现盈亏的近似)."""
    try:
        from .risk_control import account_snapshot

        snap = account_snapshot()
        total = float(snap.get("total_assets") or 0)
        pnl = float(snap.get("daily_pnl") or 0)
        if total <= 0:
            return None
        return round(pnl / total, 6)
    except Exception:
        return None


def _data_source_health() -> list[dict]:
    """数据源健康矩阵快照 (失败时不阻断日报)."""
    try:
        from ai_stock.dataflows.data_health import snapshot

        return snapshot()
    except Exception:
        return []
