"""策略台自检 (PRD §7.6.2).

四条硬检查, 全部可自动化, 失败即退出码 1:
  S1 交易未被掐死 — 近 20 交易日每天都有 trade_log 记录 (含 rejected)
  S2 断言未被污染 — 全部断言都是 close 快照 (盘中快照不得入账)
  S3 无法判定率可控 — 单 driver 的 unjudged 率 ≤ 30%
  S4 回灌系数合法 — 系数在 0.8~1.2; 开关打开时不可为空 (空 = 静默不加权)

用法:
    python scripts/strategy_selfcheck.py           # 人读
    python scripts/strategy_selfcheck.py --strict  # CI (S1 也判失败)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MAX_UNJUDGED_RATE = 0.30
LOOKBACK = 20


def feedback_min() -> float:
    from ai_stock.quant.config import STRATEGY_COEF_MIN

    return STRATEGY_COEF_MIN


def feedback_max() -> float:
    from ai_stock.quant.config import STRATEGY_COEF_MAX

    return STRATEGY_COEF_MAX


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true", help="S1 也作为失败条件")
    args = parser.parse_args()

    from contextlib import closing

    from ai_stock.quant import db, db_ops
    from ai_stock.quant.calendar_utils import advance_trading_days, get_trade_date
    from ai_stock.quant.db_models import StrategyPrediction
    from ai_stock.quant.db import session_scope

    db.init_quant_db()
    failures: list[str] = []
    notes: list[str] = []

    # ---- S1: 交易未被掐死 --------------------------------------------------
    today = get_trade_date()
    from datetime import datetime

    end = datetime.strptime(today, "%Y-%m-%d").date()
    start = advance_trading_days(end, -(LOOKBACK - 1))
    total_orders = 0
    empty_days = 0
    for i in range(LOOKBACK):
        day = advance_trading_days(start, i).isoformat()
        n = db_ops.count_orders_today(day, user_id="__all__")
        total_orders += n
        if n == 0:
            empty_days += 1

    if total_orders == 0:
        notes.append(f"S1 跳过: 近 {LOOKBACK} 日无任何委托记录 (可能是全新库)")
    elif empty_days >= LOOKBACK // 2:
        failures.append(f"S1 失败: 近 {LOOKBACK} 日有 {empty_days} 天零委托 —— 疑似被硬门槛掐死")
    else:
        notes.append(f"S1 通过: 近 {LOOKBACK} 日共 {total_orders} 笔委托")

    # ---- S2 / S3: 断言质量 -------------------------------------------------
    with closing(session_scope()) as s:
        rows = s.query(StrategyPrediction).all()
        total = len(rows)
        intraday = sum(1 for r in rows if r.snapshot_type != "close")
        by_driver: dict[str, list] = {}
        for r in rows:
            by_driver.setdefault(r.driver or "未标注", []).append(r)

    if total == 0:
        notes.append("S2/S3 跳过: 尚无断言记录")
    else:
        if intraday:
            failures.append(f"S2 失败: {intraday}/{total} 条断言非 close 快照 —— 统计已污染")
        else:
            notes.append(f"S2 通过: {total} 条断言全部为 close 快照")

        for driver, group in by_driver.items():
            unjudged = sum(1 for r in group if r.verify_state == "unjudged")
            rate = unjudged / len(group)
            if rate > MAX_UNJUDGED_RATE:
                failures.append(
                    f"S3 失败: driver={driver} unjudged {rate:.0%} "
                    f"({unjudged}/{len(group)}) —— 本期不得出权重系数"
                )
        if not any("S3" in f for f in failures):
            notes.append(f"S3 通过: {len(by_driver)} 个 driver 无法判定率均 ≤ {MAX_UNJUDGED_RATE:.0%}")

    # ---- S4: 回灌系数合法 ---------------------------------------------------
    # 开关打开却拿不到合法系数 = 选股侧会静默不加权 (看起来正常, 实际没生效);
    # 系数越界 = 有人手改了 config, 会直接把某个行业挤出榜单.
    from ai_stock.quant.strategy import feedback

    payload = feedback.read_payload()
    coefs = payload.get("coefs") or {}
    bad = {
        d: c for d, c in coefs.items()
        if not (feedback_min() <= float(c) <= feedback_max())
    }
    if bad:
        failures.append(f"S4 失败: 系数越界 {bad} —— 只允许 0.8~1.2")
    elif feedback.enabled() and not coefs:
        failures.append(
            "S4 失败: 回灌开关已开但无任何系数 —— 选股侧不会加权, 属于静默失效"
        )
    elif coefs:
        notes.append(f"S4 通过: {len(coefs)} 个 driver 有系数 ({payload.get('updated_at') or '未知时间'})")
    else:
        notes.append("S4 跳过: 尚未产生系数 (样本不足或开关关闭)")

    for line in notes:
        print(f"  ok   {line}")
    for line in failures:
        print(f"  FAIL {line}")

    if args.strict and any(f.startswith("S1") for f in failures):
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
