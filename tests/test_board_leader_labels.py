"""行业榜龙头标签的判定口径（领涨 / 精选）。

这条规则看着小，但它决定"哪个是当日最强"这个最该有的信息是否存在于界面上：
前端只显示标签字面值，语义写错就等于给用户一个假信息。
"""

from __future__ import annotations

import pytest

from ai_stock.quant.agents.selection import leader_board_labels


def test_lingzhang_requires_beating_the_board():
    """「领涨」= 涨幅第一 **且** 跑赢板块。

    只判"第一"不够: 整块在跌时"第一"仍是负收益，标"领涨"就是撒谎。
    """
    leaders = [
        {"code": "A", "change_pct": -0.5},
        {"code": "B", "change_pct": -2.0},
    ]
    rows = leader_board_labels(leaders, board_pct=-1.2, picked_codes=set())
    by_code = {r["code"]: r for r in rows}
    assert by_code["A"]["leader_label"] == "领涨"      # -0.5 > -1.2，跑赢板块
    assert by_code["B"]["leader_label"] == ""


def test_no_lingzhang_when_board_falls_faster_than_every_member():
    """板块跌 1.2% 但所有成分股都跌得更多 → 没有领涨，不许硬选一个。"""
    leaders = [
        {"code": "A", "change_pct": -3.0},
        {"code": "B", "change_pct": -4.0},
    ]
    rows = leader_board_labels(leaders, board_pct=-1.2, picked_codes=set())
    assert all(r["leader_label"] == "" for r in rows)


def test_no_lingzhang_without_board_pct():
    """没有板块涨跌幅时不能判断"跑赢"，只能不打标签（缺失 ≠ 平盘）。"""
    leaders = [{"code": "A", "change_pct": 5.0}]
    rows = leader_board_labels(leaders, board_pct=None, picked_codes=set())
    assert rows[0]["leader_label"] == ""
    assert rows[0]["labels"] == []


def test_both_labels_keep_full_set_and_prioritise_lingzhang():
    """已入池又涨幅第一：两个标签都成立，主标签取「领涨」，完整集合给前端并列展示。"""
    leaders = [
        {"code": "A", "change_pct": 6.0},
        {"code": "B", "change_pct": 3.0},
    ]
    rows = leader_board_labels(leaders, board_pct=2.0, picked_codes={"A"})
    by_code = {r["code"]: r for r in rows}
    assert by_code["A"]["labels"] == ["领涨", "精选"]
    assert by_code["A"]["leader_label"] == "领涨"
    assert by_code["B"]["labels"] == []


def test_selected_only_is_labelled_jingxuan():
    leaders = [{"code": "A", "change_pct": 1.0}]
    rows = leader_board_labels(leaders, board_pct=1.0, picked_codes={"A"})
    # 涨幅等于板块，没跑赢 → 只有「精选」
    assert rows[0]["labels"] == ["精选"]
    assert rows[0]["leader_label"] == "精选"


def test_labels_do_not_mutate_input():
    leaders = [{"code": "A", "change_pct": 6.0}]
    leader_board_labels(leaders, board_pct=1.0, picked_codes=set())
    assert "leader_label" not in leaders[0]
