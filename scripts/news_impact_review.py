"""新闻影响力回测：把台账里的预测与事后实际涨跌对照.

用法::

    python scripts/news_impact_review.py            # 回填到期样本并打印汇总
    python scripts/news_impact_review.py --verdays 1
    python scripts/news_impact_review.py --report   # 只读，不回填

口径
----
- 基准日 ``base_date``：落榜当天（快照生成日）
- 验证窗口：``horizon`` 1d / 3d / 1w → 基准日后第 1 / 3 / 5 个交易日
- 实际收益：主题成分股**等权**收盘价收益（与体检闸门的等权口径一致，
  不用市值加权 —— 否则一只大市值龙头会淹没整个主题的方向）
- 方向命中：bullish 且 ret > 0 / bearish 且 ret < 0；neutral 不计入方向命中率
- 区间命中：``exp_low <= ret <= exp_high``（本来就是区间预测，宽区间更容易命中，
  所以单独统计，不和方向命中率混在一起看）

为什么必须等权：主题的价值是"这组票会一起动"，市值加权会让一次判断退化成
对龙头的判断；方向命中率是用来校验判断链的，不是用来算组合收益的。
"""

from __future__ import annotations

import argparse
import statistics
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HORIZON_DAYS = {"1d": 1, "3d": 3, "1w": 5}


def _session():
    from ai_stock.pipeline.db_ops import _get_session

    return _get_session()


def _pending(limit: int = 0) -> list[dict]:
    """取未验证且已过基准日的台账行。"""
    from sqlalchemy import text

    sql = (
        "SELECT id, theme, direction, horizon, base_date, exp_low, exp_high, title "
        "FROM news_impact_tracking WHERE actual_return IS NULL AND base_date < :today "
        "ORDER BY id"
    )
    if limit:
        sql += " LIMIT %d" % int(limit)
    s = _session()
    try:
        rows = s.execute(
            text(sql), {"today": date.today().isoformat()}
        ).fetchall()
    finally:
        s.close()
    return [
        {
            "id": r[0], "theme": r[1], "direction": r[2], "horizon": r[3],
            "base_date": r[4], "exp_low": r[5], "exp_high": r[6], "title": r[7],
        }
        for r in rows
    ]


def _theme_members() -> dict[str, list[str]]:
    """主题名 → 成分股代码（含 active 与 rejected，复盘只看历史预测本身）。"""
    from sqlalchemy import text

    s = _session()
    try:
        rows = s.execute(text(
            "SELECT t.name, s.symbol FROM quant_theme t "
            "JOIN quant_theme_stock s ON s.theme_id = t.id"
        )).fetchall()
    finally:
        s.close()
    out: dict[str, list[str]] = {}
    for name, symbol in rows:
        out.setdefault(name, []).append(symbol)
    return out


_CLOSES: dict[str, list[tuple[str, float]]] = {}


def _closes(symbol: str) -> list[tuple[str, float]]:
    """最近 120 个交易日的 (日期, 收盘)；带进程内缓存，避免同票重复拉取。"""
    if symbol in _CLOSES:
        return _CLOSES[symbol]
    from ai_stock.quant.data_service import get_data_service

    series: list[tuple[str, float]] = []
    try:
        df = get_data_service().get_ohlcv(symbol, 130)
        if df is not None and len(df):
            cols = {str(c).lower(): c for c in df.columns}
            date_col = cols.get("date") or cols.get("trade_date") or df.columns[0]
            close_col = cols.get("close")
            if close_col is not None:
                for _, row in df.iterrows():
                    d = str(row[date_col])[:10]
                    try:
                        series.append((d, float(row[close_col])))
                    except (TypeError, ValueError):
                        continue
    except Exception:
        series = []
    _CLOSES[symbol] = series
    return series


def _window_return(symbols: list[str], base_date: str, days: int) -> float | None:
    """基准日收盘 → 第 *days* 个交易日收盘的等权收益（小数）。"""
    rets = []
    for code in symbols:
        series = _closes(code)
        if len(series) <= days:
            continue
        idx = next((i for i, (d, _) in enumerate(series) if d >= base_date), None)
        if idx is None or idx + days >= len(series):
            continue
        base = series[idx][1]
        if base <= 0:
            continue
        rets.append(series[idx + days][1] / base - 1.0)
    if not rets:
        return None
    return statistics.fmean(rets)


def _judge(direction: str, ret: float) -> int:
    if direction == "bullish":
        return int(ret > 0)
    if direction == "bearish":
        return int(ret < 0)
    return 0


def backfill(max_rows: int = 500) -> dict:
    """回填所有到期样本，返回统计。"""
    from sqlalchemy import text

    members = _theme_members()
    rows = _pending(limit=max_rows)
    filled = 0
    s = _session()
    try:
        for row in rows:
            days = HORIZON_DAYS.get(row["horizon"], 1)
            codes = members.get(row["theme"]) or []
            if not codes:
                continue
            ret = _window_return(codes, row["base_date"], days)
            if ret is None:
                continue
            hit = _judge(row["direction"], ret)
            in_range = int((row["exp_low"] or 0) / 100.0 <= ret <= (row["exp_high"] or 0) / 100.0)
            s.execute(
                text(
                    "UPDATE news_impact_tracking SET actual_return = :ret, "
                    "direction_hit = :hit, range_hit = :rg, verified_at = :now WHERE id = :id"
                ),
                {
                    "ret": round(ret, 6), "hit": hit, "rg": in_range,
                    "now": datetime.utcnow(), "id": row["id"],
                },
            )
            filled += 1
        s.commit()
    finally:
        s.close()
    return {"pending": len(rows), "filled": filled}


def report() -> None:
    """按主题打印方向命中率与区间覆盖率。"""
    from sqlalchemy import text

    s = _session()
    try:
        rows = s.execute(text(
            "SELECT theme, direction, COUNT(*), "
            "       SUM(CASE WHEN direction_hit = 1 THEN 1 ELSE 0 END), "
            "       SUM(CASE WHEN range_hit = 1 THEN 1 ELSE 0 END), "
            "       AVG(actual_return) "
            "FROM news_impact_tracking WHERE actual_return IS NOT NULL "
            "GROUP BY theme, direction ORDER BY COUNT(*) DESC"
        )).fetchall()
        total = s.execute(text(
            "SELECT COUNT(*) FROM news_impact_tracking"
        )).scalar() or 0
        verified = s.execute(text(
            "SELECT COUNT(*) FROM news_impact_tracking WHERE actual_return IS NOT NULL"
        )).scalar() or 0
    finally:
        s.close()

    print("台账 %d 行，已验证 %d 行，待验证 %d 行\n" % (total, verified, total - verified))
    if not rows:
        print("暂无已验证样本。需要至少一个完整交易日的后续行情。")
        return

    print("%-24s %-8s %5s %10s %10s %10s" % ("主题", "方向", "样本", "方向命中", "区间命中", "平均实际"))
    print("-" * 74)
    for theme, direction, n, hit, rg, avg in rows:
        if direction == "neutral":
            continue
        hit_rate = (hit or 0) / n if n else 0.0
        rg_rate = (rg or 0) / n if n else 0.0
        print("%-24s %-8s %5d %9.0f%% %9.0f%% %9.2f%%" % (
            theme, direction, n, 100 * hit_rate, 100 * rg_rate, 100 * (avg or 0)))

    all_rows = [r for r in rows if r[1] != "neutral"]
    n = sum(r[2] for r in all_rows)
    hit = sum(r[3] or 0 for r in all_rows)
    rg = sum(r[4] or 0 for r in all_rows)
    if n:
        print("-" * 74)
        print("%-24s %-8s %5d %9.0f%% %9.0f%%" % ("合计", "方向", n, 100 * hit / n, 100 * rg / n))
        print("\n判读标准：方向命中率 < 55%% 说明主题映射或方向判断需要重审；"
              "\n区间覆盖率 < 50%% 说明校准系数偏窄（STRENGTH_COEF/elasticity）。")


def main() -> None:
    ap = argparse.ArgumentParser(description="新闻影响力回测")
    ap.add_argument("--report", action="store_true", help="只出报表，不回填")
    ap.add_argument("--max", type=int, default=500, help="单次最多回填行数")
    args = ap.parse_args()

    if not args.report:
        res = backfill(max_rows=args.max)
        print("到期 %d 行，成功回填 %d 行\n" % (res["pending"], res["filled"]))
    report()


if __name__ == "__main__":
    main()
