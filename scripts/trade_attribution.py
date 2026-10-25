"""交易链路归因报表（**只读**）.

为什么需要它: 模拟盘跑了几个月, 系统能回答「赚了多少」, 但回答不了投研真正关心的
三个问题 —— 哪一类信号在赚钱、哪一环把机会拒掉了、退出意图平均要挂多久才成交。
没有这层归因, 调阈值只能靠"观察几天感觉一下", 参数会越调越随机。

本脚本只做三件事, 全部基于**已有**数据:

1. 决策漏斗: 按 (agent, decision) 统计最近 N 天的决策分布, 看链路卡在哪一步;
2. 拒绝原因: 把 ``second_verification`` 的 ``reject_stage`` 拆开计数;
3. 成交质量: 已实现盈亏、胜率、盈亏比、卖出原因分布。

**约束**: 不写库、不发 LLM、不抓行情、不改任何配置。所以它可以在生产上随时跑,
也可以进 cron 生成周报。

用法（部署机，与其它运维脚本同一套环境加载方式）::

    cd /deploy/AI_stock/backend
    sudo -u www-data bash -c 'set -a; . /deploy/AI_stock/.env; set +a; \\
        export HOME=/home/www-data PROJECT_ROOT=/deploy/AI_stock; \\
        cd /deploy/AI_stock/backend; \\
        .venv/bin/python /deploy/AI_stock/scripts/trade_attribution.py --days 30'

    # 机器可读 (给看板/周报用)
    ... trade_attribution.py --days 7 --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("PROJECT_ROOT", str(ROOT))

_MARKET_TZ = timezone(timedelta(hours=8))


def _parse_ts(raw) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _decision_funnel(rows: list[dict]) -> list[dict]:
    """按 (agent, decision) 计数, 按次数降序。"""
    counter: Counter = Counter()
    for row in rows:
        counter[(row.get("agent") or "?", row.get("decision") or "?")] += 1
    return [
        {"agent": agent, "decision": decision, "count": count}
        for (agent, decision), count in counter.most_common()
    ]


def _reject_stages(rows: list[dict]) -> dict:
    """二次验证的拒绝原因分布 + 通过率。

    只统计 ``second_verification``: 其它环节的 ``reject_stage`` 语义不同,
    混在一起会把"LLM 判不通过"和"硬性风控拒单"算进同一个分母。
    """
    stages: Counter = Counter()
    total = 0
    for row in rows:
        if (row.get("agent") or "") != "second_verification":
            continue
        decision = row.get("decision") or ""
        if decision not in ("verification_passed", "verification_rejected"):
            continue
        total += 1
        if decision == "verification_passed":
            stages["passed"] += 1
            continue
        stage = (row.get("detail") or {}).get("reject_stage") or "unspecified"
        stages[stage] += 1
    return {
        "total": total,
        "stages": dict(stages.most_common()),
        "pass_rate": round(stages["passed"] / total, 4) if total else None,
    }


def _trade_stats(trades: list[dict]) -> dict:
    """成交与已实现盈亏 (只统计 filled)。"""
    filled = [t for t in trades if (t.get("status") or "") == "filled"]
    buys = [t for t in filled if t.get("side") == "buy"]
    sells = [t for t in filled if t.get("side") == "sell"]
    pnls = [float(t.get("realized_pnl") or 0.0) for t in sells]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    return {
        "filled_orders": len(filled),
        "buy_orders": len(buys),
        "sell_orders": len(sells),
        "rejected_orders": len([t for t in trades if (t.get("status") or "") == "rejected"]),
        "realized_pnl": round(sum(pnls), 2),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(len(wins) / len(sells), 4) if sells else None,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        # 盈亏比: 平均盈利 / 平均亏损 (无亏损样本时给 None, 不写成 inf 骗自己)
        "profit_factor": round(avg_win / avg_loss, 3) if avg_loss > 0 else None,
    }


# 卖出原因里的数字都是**参数**, 不是分类维度:
#   "硬性止损: 浮亏 5.1%" 与 "硬性止损: 浮亏 5.4%" 必须归并成一类,
# 按固定字数截断会把它们拆成两个桶, 报表就失去了看"止损/止盈各占多少"的意义。
_REASON_NOISE = re.compile(r"[\d.,%+\-_]+")
_REASON_BUCKET_MAX = 16


def _reason_bucket(reason: str) -> str:
    """取 reason 的语义前缀 (第一个数字之前的部分) 作为归并键。"""
    raw = (reason or "").strip()
    if not raw:
        return "未记录"
    head = _REASON_NOISE.split(raw, maxsplit=1)[0].strip(" :：,，、/·-—")
    # 以数字开头的原因 (如 "5% 止损") 拿不到前缀, 退回原文以免归到"未记录"
    return (head or raw)[:_REASON_BUCKET_MAX]


def _sell_reason_mix(trades: list[dict]) -> list[dict]:
    """卖出原因分布 (按语义前缀归并, 便于看"止损/止盈/择时"占比)。"""
    counter: Counter = Counter()
    for t in trades:
        if (t.get("side") or "") != "sell" or (t.get("status") or "") != "filled":
            continue
        counter[_reason_bucket(t.get("reason"))] += 1
    return [{"reason": k, "count": v} for k, v in counter.most_common(12)]


def _pending_exit_watch(holdings: list[dict]) -> list[dict]:
    """当前仍挂着的退出意图 (卖不掉的持仓) —— 这是最该人工看一眼的清单。"""
    from ai_stock.quant import exit_state

    out = []
    for holding in holdings:
        pending = exit_state.get_pending_exit(holding)
        if not pending:
            continue
        out.append({
            "symbol": holding.get("symbol", ""),
            "reason": pending.get("reason", ""),
            "portion": pending.get("portion"),
            "fail_count": pending.get("fail_count"),
            "confirm_needed": pending.get("confirm_needed"),
            "first_at": pending.get("first_at"),
            "last_error": pending.get("last_error", ""),
        })
    return out


def collect(days: int) -> dict:
    """汇总最近 ``days`` 天的归因数据 (纯读)."""
    from ai_stock.quant import db_ops

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    decisions = db_ops.get_decisions(limit=20000, since=cutoff)
    # trade_log 没有 since 参数 (它是 broker 状态重建的热路径, 不为此加过滤),
    # 报表量级很小, 在内存里按时间过滤即可
    trades = [
        t for t in db_ops.get_trades(limit=0, user_id="__all__")
        if (_parse_ts(t.get("trade_time")) or cutoff) >= cutoff
    ]
    holdings = db_ops.get_holdings()

    return {
        "window_days": days,
        "since": cutoff.isoformat(timespec="seconds"),
        "decisions": len(decisions),
        "funnel": _decision_funnel(decisions),
        "verification": _reject_stages(decisions),
        "trades": _trade_stats(trades),
        "sell_reasons": _sell_reason_mix(trades),
        "pending_exit": _pending_exit_watch(holdings),
    }


def _fmt_pct(value) -> str:
    return "—" if value is None else f"{value:.1%}"


def render(report: dict) -> str:
    """人读版报表 (一眼看漏斗卡在哪 + 卖得怎么样)."""
    lines: list[str] = []
    trades = report["trades"]
    verification = report["verification"]

    lines.append(f"交易链路归因 · 最近 {report['window_days']} 天 "
                 f"(since {report['since'][:10]})")
    lines.append(f"决策日志 {report['decisions']} 条 · 委托 {trades['filled_orders']} 笔成交 "
                 f"/ {trades['rejected_orders']} 笔拒单")
    lines.append("")

    lines.append("① 二次验证漏斗")
    lines.append(f"   进去 {verification['total']} 次, 通过率 {_fmt_pct(verification['pass_rate'])}")
    for stage, count in verification["stages"].items():
        lines.append(f"   - {stage:<22} {count}")
    lines.append("")

    lines.append("② 成交与盈亏 (已实现)")
    lines.append(f"   买 {trades['buy_orders']} / 卖 {trades['sell_orders']} · "
                 f"盈亏 {trades['realized_pnl']:+.2f} 元")
    lines.append(f"   胜率 {_fmt_pct(trades['win_rate'])} "
                 f"({trades['win_count']}胜 {trades['loss_count']}负) · "
                 f"平均盈利 {trades['avg_win']:.2f} / 平均亏损 {trades['avg_loss']:.2f} · "
                 f"盈亏比 {trades['profit_factor'] if trades['profit_factor'] else '—'}")
    lines.append("")

    lines.append("③ 卖出原因分布")
    for item in report["sell_reasons"]:
        lines.append(f"   - {item['reason']:<14} {item['count']}")
    if not report["sell_reasons"]:
        lines.append("   (窗口内无卖出成交)")
    lines.append("")

    lines.append("④ 仍挂着的退出意图 (卖不掉 / 待确认)")
    if report["pending_exit"]:
        for item in report["pending_exit"]:
            lines.append(
                f"   - {item['symbol']} 原因={item['reason'] or '?'} "
                f"比例={item['portion']} 失败={item['fail_count']} "
                f"待确认={item['confirm_needed']} "
                f"首次={str(item['first_at'])[:19]} 最近错误={item['last_error'] or '—'}"
            )
    else:
        lines.append("   (无)")
    lines.append("")

    lines.append("⑤ 决策分布 (agent / decision, 前 20)")
    for item in report["funnel"][:20]:
        lines.append(f"   - {item['agent']:<20} {item['decision']:<22} {item['count']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="交易链路归因报表 (只读)")
    parser.add_argument("--days", type=int, default=30, help="回看天数 (默认 30)")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非文本")
    args = parser.parse_args(argv)

    from ai_stock.quant import db

    db.init_quant_db()          # 幂等建表, 不改数据
    report = collect(max(1, args.days))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
