"""三榜成绩单 (只读)。

设计纪律(照抄 PRD, 任何一条都不能省):
1. **分母只算已判定样本** —— 没到验证日的不进分母, 否则"还没涨"会被算成
   "判断错"; 数据缺失记「无法判定」并排除, 不把接口故障算成判断失误。
2. **样本 < 30 不出胜率数字** —— 只报样本量与"样本不足", 不给一个看起来
   精确但随机波动的百分比。
3. **只读, 不回写任何阈值** —— 一旦让它自动调参, 就会重演"单一日频信号
   驱动全局参数"的死锁。成绩单用来看, 不用来改。

三个榜的口径不同, 因为它们预测的东西不同: 新闻榜预测"方向与幅度", 行业榜
预测"排序", 热股榜预测"这只票"。用同一个指标衡量三者只会得到没有意义的
平均数。
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

MIN_SAMPLES = 30          # 低于此数不出胜率(纪律 2)
INDUSTRY_WINDOW = 5       # 行业榜: 5 个交易日
INDUSTRY_TOP_N = 5        # 行业榜: 只看 Top5
POOL_WINDOW = 5           # 热股榜: 5 个交易日
MAX_PRICE_SYMBOLS = 20    # 收益口径最多取多少个标的(网络成本上限)


def build_scorecard(days: int = 30, include_returns: bool = False) -> dict:
    """三榜成绩单。``include_returns`` 打开时才算热股榜的 5 日收益(要拉 K 线)。"""
    return {
        "window_days": days,
        "min_samples": MIN_SAMPLES,
        "news": _news(days),
        "industry": _industry(),
        "pool": _pool(include_returns=include_returns),
    }


# ---------------------------------------------------------------------------
# 新闻榜: 方向 / 区间命中率
# ---------------------------------------------------------------------------


def _news(days: int) -> dict:
    from ai_stock.pipeline.db_ops import get_news_tracking_stats

    stats = get_news_tracking_stats(days)
    if not stats.get("ok"):
        return {"ok": False, "reason": stats.get("reason", "未知原因")}

    def _gate(block: dict) -> dict:
        n = block.get("n") or 0
        return {**block, "sample_ok": n >= MIN_SAMPLES}

    return {
        "ok": True,
        "metric": "主题级方向命中率与预期区间命中率（成分股等权）",
        "window_days": stats.get("window_days", days),
        "total": stats.get("total", 0),
        "judged": stats.get("judged", 0),
        "pending": stats.get("pending", 0),
        "overall": _gate(stats.get("overall") or {}),
        "by_event_type": {
            k: _gate(v) for k, v in (stats.get("by_event_type") or {}).items()
        },
        "by_theme": {
            k: _gate(v) for k, v in (stats.get("by_theme") or {}).items()
        },
    }


# ---------------------------------------------------------------------------
# 行业榜: Top5 的 5 日累计 vs 全行业等权
# ---------------------------------------------------------------------------


def _industry() -> dict:
    from datetime import date, timedelta

    from ai_stock.quant.db_ops import get_industry_boards_since

    start = (date.today() - timedelta(days=120)).isoformat()
    history = get_industry_boards_since(start, limit_dates=60)
    result = industry_scorecard(history)
    return {
        "ok": True,
        "metric": (
            f"Top{INDUSTRY_TOP_N} 行业此后 {INDUSTRY_WINDOW} 个交易日的累计涨跌幅，"
            "相对同期全行业等权均值（超额）"
        ),
        **result,
    }


def industry_scorecard(history: Sequence[dict]) -> dict:
    """行业榜排序的有效性。

    *history* 新→旧。对每一个"更早、且后面已有 ``INDUSTRY_WINDOW`` 个交易日"
    的期次(索引 i, 未来窗口 = ``history[i-WINDOW:i]``), 取当期 Top5, 累加这些
    行业在随后各日的 ``change_pct``; 基准取**入榜当日全部在榜行业**的同窗口
    等权累计涨幅(拿不到指数, 用横截面均值作代理, 并在 ``metric`` 里写明)。

    只用板块自身日涨跌幅做累加, **不引入新的行情依赖** —— 行业榜本来就按日
    存了 ``change_pct``, 这一层零网络。
    """
    if len(history) <= INDUSTRY_WINDOW:
        return _empty("行业榜历史不足，无法构成 5 日窗口")

    pct: dict[tuple[str, str], float] = {}
    for snap in history:
        for row in snap.get("rows") or []:
            name = str(row.get("industry") or "")
            value = row.get("change_pct")
            if name and value is not None:
                pct[(snap["rank_date"], name)] = float(value)

    samples: list[float] = []
    benchmarks: list[float] = []
    for i in range(INDUSTRY_WINDOW, len(history)):
        entry = history[i]
        window = history[i - INDUSTRY_WINDOW:i]      # 入榜之后的 N 个交易日
        if len(window) < INDUSTRY_WINDOW:
            continue
        top = [
            r for r in entry.get("rows") or []
            if int(r.get("rank") or 0) <= INDUSTRY_TOP_N
        ]
        if not top:
            continue

        def _cum(name: str) -> Optional[float]:
            total = 0.0
            for w in window:
                value = pct.get((w["rank_date"], name))
                if value is None:
                    return None
                total += value
            return total

        # 基准 = 入榜当日全部在榜行业的等权累计涨幅
        names = {str(r.get("industry") or "") for r in entry.get("rows") or []}
        base_values = [v for v in (_cum(n) for n in names if n) if v is not None]
        if not base_values:
            continue
        base = sum(base_values) / len(base_values)

        for row in top:
            cum = _cum(str(row.get("industry") or ""))
            if cum is None:
                continue
            samples.append(cum - base)
            benchmarks.append(base)

    n = len(samples)
    if not n:
        return _empty("没有可判定的样本（行业未连续在榜或涨跌幅缺失）")

    hit = sum(1 for x in samples if x > 0)
    return {
        "ok": True,
        "n": n,
        "sample_ok": n >= MIN_SAMPLES,
        "hit_rate": round(hit / n, 3) if n >= MIN_SAMPLES else None,
        "avg_excess_pct": round(sum(samples) / n, 2),
        "best_excess_pct": round(max(samples), 2),
        "worst_excess_pct": round(min(samples), 2),
        "avg_benchmark_pct": round(sum(benchmarks) / len(benchmarks), 2),
    }


def _empty(reason: str) -> dict:
    return {"ok": False, "reason": reason, "n": 0, "sample_ok": False}


# ---------------------------------------------------------------------------
# 热股榜: 存活率 / 衰竭 / 榜首被买入比例
# ---------------------------------------------------------------------------


def _pool(include_returns: bool = False) -> dict:
    from ai_stock.quant.db_ops import (
        get_pool_snapshot,
        list_pool_snapshot_dates,
    )

    dates = list_pool_snapshot_dates()
    survival = pool_survival(dates, get_pool_snapshot)
    bought = _top_bought_ratio(dates, get_pool_snapshot)

    block = {
        "ok": survival["n"] > 0,
        "metric": (
            f"入池后 {POOL_WINDOW} 个交易日的存活率（仍 active 的比例）、"
            "鱼尾标记率，以及榜首 5 条被买入的比例"
        ),
        **survival,
        "top_bought": bought,
    }
    if include_returns:
        block["returns"] = _pool_returns(dates, get_pool_snapshot)
    else:
        block["returns"] = {
            "ok": False,
            "reason": "未启用（收益口径需逐只拉 K 线，默认关闭，用 ?include_returns=1 开启）",
        }
    return block


def pool_survival(dates: Sequence[str], loader) -> dict:
    """入池 N 日后仍 active 的比例 + 鱼尾标记率。

    这是纯库内计算: 只需要日快照, 不需要行情。它回答的是"这个榜选出来的票
    能不能活下来", 而不是"赚不赚钱"—— 后者需要 K 线, 见 ``_pool_returns``。
    """
    dates = sorted({d for d in dates if d}, reverse=True)
    if len(dates) <= POOL_WINDOW:
        return {"n": 0, "reason": "池快照不足，无法构成 5 日窗口"}

    entries: list[dict] = []
    alive = 0
    tailed = 0
    # dates 新→旧: 入池日是更早的那一期(i+WINDOW), 对照日是此后第 WINDOW 期(i)
    for i in range(0, len(dates) - POOL_WINDOW):
        entry_date = dates[i + POOL_WINDOW]
        later_date = dates[i]
        entry_rows = loader(entry_date)
        later_rows = loader(later_date)
        if not entry_rows or not later_rows:
            continue
        later_map = {r.get("symbol"): r for r in later_rows}
        for row in entry_rows:
            symbol = row.get("symbol")
            if not symbol:
                continue
            entries.append(row)
            after = later_map.get(symbol)
            if after and after.get("status") == "active":
                alive += 1
            if after and after.get("stage_batch") == "TAIL":
                tailed += 1

    n = len(entries)
    if not n:
        return {"n": 0, "reason": "没有可判定的样本（快照存在但无活跃标的）"}
    return {
        "n": n,
        "sample_ok": n >= MIN_SAMPLES,
        "window": POOL_WINDOW,
        "alive_n": alive,
        "tailed_n": tailed,
        "survival_rate": round(alive / n, 3) if n >= MIN_SAMPLES else None,
        "tail_rate": round(tailed / n, 3) if n >= MIN_SAMPLES else None,
        "avg_dyn": round(
            sum(float(r.get("dyn_confidence") or 0) for r in entries) / n, 3,
        ),
    }


def _top_bought_ratio(dates: Sequence[str], loader) -> dict:
    """榜首 5 条被买入的比例 —— 设计文档 §6 指定的**反向指标**。

    如果长期接近 0，说明榜单与交易是两套系统，那么所有展示优化都只是把
    橱窗擦得更亮。所以它必须出现在成绩单里，而不是只写在文档里。

    口径 = "入池之后出现过买入记录"（用每只票的最早买入日与快照日比较），
    而不是"曾经被买过" —— 后者会把入池之前的历史成交也算进来，把比例做高。
    被拒单(rejected)不算买入。
    """
    from ai_stock.quant import db_ops

    dates = sorted({d for d in dates if d}, reverse=True)
    if not dates:
        return {"ok": False, "reason": "无池快照"}

    try:
        trades = db_ops.get_trades(limit=5000)
    except Exception as exc:
        logger.warning("trade log read failed: %s", exc)
        return {"ok": False, "reason": str(exc)}

    first_buy: dict[str, str] = {}
    for row in trades:
        if str(row.get("side") or "").lower() != "buy":
            continue
        if str(row.get("status") or "").lower() == "rejected":
            continue
        symbol = str(row.get("symbol") or "")
        day = str(row.get("trade_time") or "")[:10]
        if not symbol or not day:
            continue
        if symbol not in first_buy or day < first_buy[symbol]:
            first_buy[symbol] = day

    top_total = 0
    top_bought = 0
    for date_str in dates[:20]:
        for row in loader(date_str)[:5]:
            symbol = str(row.get("symbol") or "")
            if not symbol:
                continue
            top_total += 1
            if first_buy.get(symbol, "9999-12-31") >= date_str:
                top_bought += 1
    if not top_total:
        return {"ok": False, "reason": "无样本"}
    return {
        "ok": True,
        "n": top_total,
        "bought_n": top_bought,
        "sample_ok": top_total >= MIN_SAMPLES,
        "ratio": round(top_bought / top_total, 3) if top_total >= MIN_SAMPLES else None,
        "note": "长期接近 0 说明榜单与交易脱节，优先修链路而不是修展示",
    }


def _pool_returns(dates: Sequence[str], loader) -> dict:
    """入池后 5 日收益（需拉 K 线，按需开启）。

    只取最近一个"后面还有 5 个快照日"的期次、最多 ``MAX_PRICE_SYMBOLS`` 只,
    因为单次 HTTP 请求拉几十只 K 线会把接口拖成分钟级(缓存 TTL 仅 5 分钟)。
    样本不足时 ``sample_ok=False`` 且不出胜率 —— 这是纪律 2, 不是没做完。
    """
    dates = sorted({d for d in dates if d}, reverse=True)
    if len(dates) <= POOL_WINDOW:
        return {"ok": False, "reason": "池快照不足"}

    entry_date, later_date = dates[POOL_WINDOW], dates[0]
    rows = [r for r in loader(entry_date) if r.get("symbol")][:MAX_PRICE_SYMBOLS]
    if not rows:
        return {"ok": False, "reason": "该期无活跃标的"}

    try:
        from ai_stock.quant.data_service import get_data_service
    except Exception as exc:  # pragma: no cover - 依赖缺失时如实降级
        return {"ok": False, "reason": f"行情服务不可用: {exc}"}

    service = get_data_service()
    samples: list[float] = []
    for row in rows:
        try:
            df = service.get_ohlcv(str(row["symbol"]), lookback_days=30)
        except Exception as exc:
            logger.debug("ohlcv failed for %s: %s", row.get("symbol"), exc)
            continue
        ret = _window_return(df, str(entry_date), str(later_date))
        if ret is not None:
            samples.append(ret)

    n = len(samples)
    if not n:
        return {"ok": False, "reason": "K 线不可用，无法计算收益"}
    wins = sum(1 for r in samples if r > 0)
    return {
        "ok": True,
        "n": n,
        "sample_ok": n >= MIN_SAMPLES,
        "entry_date": entry_date,
        "exit_date": later_date,
        "win_rate": round(wins / n, 3) if n >= MIN_SAMPLES else None,
        "avg_return": round(sum(samples) / n, 2),
    }


def _window_return(df, start: str, end: str) -> Optional[float]:
    """闭区间 (start, end] 的收盘价涨跌幅 (%)。任一端缺数据返回 None。"""
    try:
        if df is None or len(df) == 0:
            return None
        frame = df.copy()
        frame["Date"] = frame["Date"].astype(str).str.slice(0, 10)
        base = frame[frame["Date"] <= start]
        later = frame[frame["Date"] <= end]
        if len(base) == 0 or len(later) == 0:
            return None
        start_close = float(base["Close"].iloc[-1])
        end_close = float(later["Close"].iloc[-1])
        if start_close <= 0:
            return None
        return round((end_close / start_close - 1) * 100, 2)
    except Exception:
        return None
