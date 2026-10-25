"""三榜展示口径 / 变化视图 / 成绩单的纯函数测试。

只测不碰网络的纯逻辑 —— 这些规则一旦写错，界面会给出与系统相反的信号，
而且不会报错（例如热股榜按入池静态分排序时，榜首可能是系统这轮准备踢掉的票）。
"""

from __future__ import annotations

import pytest

from app.services.board_view import (
    CHANGE_DOWN,
    CHANGE_NEW,
    CHANGE_OUT,
    CHANGE_UP,
    annotate_pool,
    consecutive_days,
    diff_ranked,
    no_previous_diff,
)
from app.services.board_scorecard import industry_scorecard, pool_survival


# ---------------------------------------------------------------------------
# 热股榜排序口径
# ---------------------------------------------------------------------------


def test_pool_sorted_by_dynamic_not_static_confidence():
    """榜首必须是动态置信度最高的那只，而不是入池分最高的那只。

    这条断言锁住三榜唯一的"展示口径 ≠ 决策口径"缺陷: buy.py 按
    dyn_confidence 排队, 榜单若按 confidence 排, 就会给出相反信号。
    """
    stocks = [
        {"symbol": "A", "confidence": 9.0, "dyn_confidence": 0.30},  # 入池看好, 已衰减
        {"symbol": "B", "confidence": 5.0, "dyn_confidence": 0.80},  # 入池一般, 正在增强
    ]
    rows = annotate_pool(stocks)
    assert [r["symbol"] for r in rows] == ["B", "A"]
    assert rows[0]["rank"] == 1
    # 两个分都保留: 差异本身就是最有用的信号
    assert rows[1]["confidence"] == 9.0
    assert rows[0]["dyn_state"] == "增强"
    assert rows[1]["dyn_state"] == "衰减"


def test_pool_missing_dyn_falls_back_to_default():
    rows = annotate_pool([{"symbol": "X", "confidence": 7.0}])
    assert rows[0]["rank"] == 1
    assert rows[0]["near_kick"] is False  # 缺字段退化到 0.5，不是"危险"


def test_pool_flags_tail_and_near_kick():
    rows = annotate_pool([
        {"symbol": "A", "dyn_confidence": 0.26, "stage_batch": "TAIL"},
        {"symbol": "B", "dyn_confidence": 0.90, "stage_batch": "BODY"},
    ])
    by_symbol = {r["symbol"]: r for r in rows}
    assert by_symbol["A"]["near_kick"] is True     # 0.26 已贴近 0.25 踢出线
    assert by_symbol["A"]["is_tail"] is True
    assert by_symbol["B"]["near_kick"] is False
    assert by_symbol["B"]["is_tail"] is False


# ---------------------------------------------------------------------------
# 通用跨期对比
# ---------------------------------------------------------------------------


def test_diff_ranked_classifies_all_four_states():
    previous = [{"industry": n} for n in ("A", "B", "C", "D")]
    current = [
        {"industry": "新来的"},
        {"industry": "B"},        # 上期第 2 → 本期第 2：不变
        {"industry": "A"},        # 上期第 1 → 本期第 3：降 2
        {"industry": "C"},        # 上期第 3 → 本期第 4：降 1
    ]
    result = diff_ranked(current, previous, key="industry")
    rows = {r["industry"]: r for r in result["rows"]}

    assert rows["新来的"]["change"] == CHANGE_NEW
    assert rows["新来的"]["prev_rank"] is None
    assert rows["B"]["change"] == "same"
    assert rows["A"]["change"] == CHANGE_DOWN
    assert rows["A"]["delta_rank"] == -2
    assert rows["C"]["delta_rank"] == -1

    # D 掉榜了
    assert [r["industry"] for r in result["dropped"]] == ["D"]
    assert result["dropped"][0]["change"] == CHANGE_OUT
    assert result["summary"]["new"] == ["新来的"]
    assert result["summary"]["has_previous"] is True


def test_diff_ranked_reports_upward_move():
    current = [{"symbol": "A"}, {"symbol": "B"}]
    previous = [{"symbol": "B"}, {"symbol": "A"}]
    rows = {r["symbol"]: r for r in diff_ranked(current, previous, key="symbol")["rows"]}
    assert rows["A"]["change"] == CHANGE_UP
    assert rows["A"]["delta_rank"] == 1


def test_no_previous_diff_does_not_fake_new_items():
    """没有上一期 ≠ 上一期是空的。混淆两者会让用户以为榜单今天大换血。"""
    result = no_previous_diff([{"industry": "X"}])
    assert result["summary"]["has_previous"] is False
    assert result["summary"]["new"] == []
    assert result["rows"][0]["industry"] == "X"


def test_consecutive_days_stops_at_first_gap():
    history = [
        {"rows": [{"industry": "A"}, {"industry": "B"}]},   # 今天
        {"rows": [{"industry": "A"}]},                      # 昨天
        {"rows": [{"industry": "C"}]},                      # 断档
        {"rows": [{"industry": "A"}]},
    ]
    assert consecutive_days(history, "A") == 2
    assert consecutive_days(history, "B") == 1
    assert consecutive_days(history, "D") == 0


# ---------------------------------------------------------------------------
# 行业榜成绩单: 窗口方向
# ---------------------------------------------------------------------------


def _history(pcts: list[dict], names: list[str]) -> list[dict]:
    """构造 history（新→旧）。``pcts[0]`` 是**最新**那一天的涨跌幅。"""
    total = len(pcts)
    return [
        {"rank_date": f"2026-10-{total - i:02d}",
         "rows": [{"rank": j + 1, "industry": n, "change_pct": pcts[i].get(n, 0.0)}
                  for j, n in enumerate(names)]}
        for i in range(total)
    ]


def test_industry_scorecard_window_points_forward_in_time():
    """入榜日必须是**更早**那一期，未来窗口取更新的日期。

    方向写反的话，成绩单会用"入榜之后的表现"去评价"入榜之前的排序"，
    而且算出来的数字看起来完全正常 —— 所以必须用一条断言把方向钉住。

    用 8 个行业、Top5 只是子集: 若 Top5 覆盖全部在榜行业，超额恒为 0
    （各自减均值必然相互抵消），那就测不出方向。
    """
    names = [f"行业{i}" for i in range(1, 9)]
    flat = {n: 0.0 for n in names}
    rising = {n: (3.0 if i < 5 else -1.0) for i, n in enumerate(names)}
    # 索引 0 最新；索引 5 最旧（= 入榜日），其后 5 天（索引 4..0）都在涨
    pcts = [rising, rising, rising, rising, rising, flat]
    result = industry_scorecard(_history(pcts, names))
    assert result["ok"] is True
    # 5 日累计: Top5 = +15%，基准 = (5*15 + 3*(-5))/8 = +7.5% → 超额 +7.5%
    assert result["avg_excess_pct"] == pytest.approx(7.5, abs=0.01)
    assert result["n"] == 5
    assert result["sample_ok"] is False        # 5 < 30，不出胜率
    assert result["hit_rate"] is None


def test_industry_scorecard_detects_underperformance():
    """方向反过来的场景必须得负超额 —— 否则"两边都正"说明口径没在看方向。"""
    names = [f"行业{i}" for i in range(1, 9)]
    flat = {n: 0.0 for n in names}
    falling = {n: (-3.0 if i < 5 else 1.0) for i, n in enumerate(names)}
    pcts = [falling, falling, falling, falling, falling, flat]
    result = industry_scorecard(_history(pcts, names))
    # 5 日累计: Top5 = -15%，基准 = (5*(-15) + 3*5)/8 = -7.5% → 超额 -7.5%
    assert result["avg_excess_pct"] == pytest.approx(-7.5, abs=0.01)


def test_industry_scorecard_needs_enough_history():
    result = industry_scorecard([{"rank_date": "2026-10-01", "rows": []}])
    assert result["ok"] is False
    assert "不足" in result["reason"]


# ---------------------------------------------------------------------------
# 热股榜成绩单: 存活率
# ---------------------------------------------------------------------------


def test_pool_survival_uses_older_entry_and_newer_comparison():
    """入池日取更早那一期，对照日取此后第 N 期。

    日期用真实格式（字符串序＝日期序），入池日是 10-01（最旧），对照日是 10-06。
    """
    dates = [f"2026-10-{i:02d}" for i in range(6, 0, -1)]   # 10-06 … 10-01

    def loader(date):
        if date == "2026-10-01":                  # 入池日（最旧）
            return [
                {"symbol": "A", "dyn_confidence": 0.7},
                {"symbol": "B", "dyn_confidence": 0.6},
                {"symbol": "C", "dyn_confidence": 0.5},
            ]
        if date == "2026-10-06":                  # 对照日（最新）
            return [
                {"symbol": "A", "status": "active", "stage_batch": "BODY"},
                {"symbol": "B", "status": "removed", "stage_batch": "BODY"},
                {"symbol": "C", "status": "active", "stage_batch": "TAIL"},
            ]
        return []

    result = pool_survival(dates, loader)
    assert result["n"] == 3
    assert result["alive_n"] == 2      # A、C 仍 active
    assert result["tailed_n"] == 1     # C 进了鱼尾
    assert result["survival_rate"] is None   # 3 < 30，只报样本不给比例


def test_pool_survival_requires_window():
    assert pool_survival(["d0", "d1"], lambda d: [])["n"] == 0
