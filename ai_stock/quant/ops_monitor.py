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
    """分级告警 + 静默窗口 (告警风暴抑制)."""

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
        return True


_alerts: Optional[AlertManager] = None


def get_alerts() -> AlertManager:
    global _alerts
    if _alerts is None:
        _alerts = AlertManager()
    return _alerts


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

    report = {
        "date": date_str,
        "generated_at": datetime.now().isoformat(),
        "global_trade_enable": db_ops.get_config_value("global_trade_enable", "0"),
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
            "## 持仓",
        ]
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
        path = report_dir() / f"quant_report_{date_str}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        report["report_file"] = str(path)
        logger.info("Daily quant report written: %s", path)
    except Exception as exc:
        logger.warning("Daily report export failed: %s", exc)

    return report
