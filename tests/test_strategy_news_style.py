"""P1/P2 补齐部分的规则单测: 新闻方向 / 风格口径 / 权重回灌.

这三块都是"宁可不结论, 不可乱结论"的地方, 所以测的重点不是"能算出数",
而是"数据不足时确实不出数".
"""

from __future__ import annotations

import pytest

from ai_stock.quant.strategy.contracts import BoardFlow, NewsImpact
from ai_stock.quant.strategy.rules import flow as flow_rules
from ai_stock.quant.strategy.rules import news as news_rules
from ai_stock.quant.strategy.rules import textmatch


# ---- 名称归一 --------------------------------------------------------------


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("CPO", "CPO概念", True),
        # 包含即匹配 (与选股侧 _match_tag 同口径); 「电力 vs 电力设备」这类
        # 反向冲突不靠匹配解决, 由 flow.classify_board 的匹配顺序解决
        ("电力设备", "电力", True),
        ("锂矿", "锂矿", True),
        ("AI", "半导体", False),          # 过短的泛名不参与核心词匹配
        ("", "CPO", False),
    ],
)
def test_match(a, b, expected):
    assert textmatch.match(a, b) is expected


def test_best_prefers_closest_length():
    """「电力设备」不能被「电力」抢走 —— 两者在资金上恰恰反向."""
    assert textmatch.best("电力设备", ["电力", "电力设备", "银行"]) == "电力设备"
    assert textmatch.best("电力", ["电力设备", "电力", "银行"]) == "电力"


def test_best_returns_empty_when_nothing_matches():
    assert textmatch.best("猪肉", ["CPO", "锂矿"]) == ""


# ---- 新闻方向 --------------------------------------------------------------


def _impact(theme, direction, strength=2, horizon="3d", base="2026-10-22",
            low=0.0, high=0.0):
    return NewsImpact(theme=theme, direction=direction, strength=strength,
                      horizon=horizon, base_date=base, exp_low=low, exp_high=high)


def test_sign_requires_min_strength():
    """单条 strength=1 的弱多不足以定方向 —— 否则每天都能凑出"利多"."""
    weak = [_impact("CPO", "bullish", strength=1)]
    assert news_rules.sign(weak) == 0
    strong = [_impact("CPO", "bullish", strength=2)]
    assert news_rules.sign(strong) == 1


def test_sign_net_off():
    items = [_impact("CPO", "bullish", 2), _impact("CPO", "bearish", 2)]
    assert news_rules.sign(items) == 0


def test_group_assigns_each_theme_once():
    items = [_impact("CPO·光模块", "bullish"), _impact("锂矿", "bearish")]
    buckets = news_rules.group(items, ["CPO·光模块", "锂矿"])
    assert len(buckets["CPO·光模块"]) == 1
    assert len(buckets["锂矿"]) == 1
    assert news_rules.coverage(buckets) == 2


def test_group_drops_unmatched_theme():
    """挂不到任何篮子上的新闻不算参与度, 否则覆盖度会虚高."""
    buckets = news_rules.group([_impact("猪肉", "bullish")], ["CPO"])
    assert news_rules.coverage(buckets) == 0


def test_catalyst_only_writes_what_was_actually_given():
    text = news_rules.catalyst([_impact("CPO", "bullish", horizon="3d", low=3, high=6)])
    assert text == "T+3 内 · 预期 +3.0~+6.0%"
    # 无 horizon 就不写日期: 编一个"预计 3 日内"是最廉价的伪精确
    assert news_rules.catalyst([_impact("CPO", "bullish", horizon="")]) == ""
    assert news_rules.catalyst([_impact("CPO", "neutral")]) == ""


def test_independent_facts_excludes_restated_tags():
    """新闻主题与涨停归因同名 = 同一事实, 不能算两个独立来源."""
    items = [_impact("CPO", "bullish")]
    assert news_rules.independent_facts(items, frozen_tags="CPO+光模块") == {
        news_rules.MARKET_FACT
    }
    assert len(news_rules.independent_facts(items, frozen_tags="")) == 2


# ---- 风格口径 --------------------------------------------------------------


def _boards(*pairs):
    return [BoardFlow(code=f"B{i}", name=n, main_net_inflow=v)
            for i, (n, v) in enumerate(pairs)]


def test_classify_board_growth_wins_over_defensive():
    """「电力设备」属成长, 「电力」属防御 —— 顺序反了新能源链会被当红利资产."""
    assert flow_rules.classify_board("电力设备") == "growth"
    assert flow_rules.classify_board("电力") == "defensive"
    assert flow_rules.classify_board("银行") == "defensive"
    assert flow_rules.classify_board("半导体") == "growth"
    assert flow_rules.classify_board("旅游") == "other"


def test_style_needs_both_sides():
    """只有成长端数据 → 不判风格 (否则每天都会得出"进攻"这个假结论)."""
    boards = _boards(("半导体", 1e8), ("电子", 1e8), ("计算机", 1e8),
                     ("通信", 1e8), ("软件", 1e8), ("旅游", -5e8))
    assert flow_rules.style_from_boards(boards) == ("", False)


def test_style_dumbbell():
    boards = _boards(
        ("半导体", 2e8), ("电子", 2e8), ("计算机", 2e8), ("通信", 2e8),
        ("银行", 1e8), ("保险", 1e8), ("煤炭", 1e8), ("电力", 1e8),
        ("旅游", -3e8), ("传媒", -3e8), ("钢铁", -3e8), ("建筑", -3e8),
    )
    label, dumbbell = flow_rules.style_from_boards(boards)
    assert dumbbell is True
    assert "哑铃" in label


def test_style_uses_average_not_sum():
    """成长端板块数量天然多于防御端, 用合计值必然偏正."""
    boards = _boards(
        # 成长端 10 个板块各 0.11 亿 → 合计 1.1 亿, 均值 0.11 亿
        *[(n, 1.1e7) for n in ("半导体", "电子", "计算机", "通信", "软件",
                               "电池", "光伏", "军工", "机器人", "医药")],
        # 防御端 4 个板块各 0.25 亿 → 合计 1.0 亿 (更小), 均值 0.25 亿 (更大)
        *[(n, 2.5e7) for n in ("银行", "保险", "电力", "煤炭")],
        ("旅游", 2e7), ("传媒", 2e7), ("钢铁", 2e7), ("建筑", 2e7),
    )
    label, dumbbell = flow_rules.style_from_boards(boards)
    assert dumbbell is False          # 中间端也是正的, 不构成哑铃
    assert label == "防御"            # 按均值判; 按合计会得出"进攻"


# ---- 权重回灌 --------------------------------------------------------------


def test_compute_coefs_maps_win_rate_linearly():
    from ai_stock.quant.strategy.feedback import compute_coefs

    coefs, skipped = compute_coefs(
        {"政策": {"judged": 40, "unjudged_rate": 0.1, "win_rate": 0.5}},
        min_samples=30, lo=0.8, hi=1.2,
    )
    assert coefs == {"政策": 1.0}
    assert skipped == {}


def test_compute_coefs_skips_low_sample():
    from ai_stock.quant.strategy.feedback import compute_coefs

    coefs, skipped = compute_coefs(
        {"情绪": {"judged": 5, "unjudged_rate": 0.0, "win_rate": 1.0}},
        min_samples=30,
    )
    assert coefs == {}
    assert "样本" in skipped["情绪"]


def test_compute_coefs_skips_bad_data_quality():
    """一次接口封禁就能把整周断言打成 unjudged —— 那天绝不该调权."""
    from ai_stock.quant.strategy.feedback import compute_coefs

    coefs, skipped = compute_coefs(
        {"产业": {"judged": 50, "unjudged_rate": 0.62, "win_rate": 0.0}},
        min_samples=30,
    )
    assert coefs == {}
    assert "无法判定率" in skipped["产业"]


def test_compute_coefs_clamped_to_bounds():
    from ai_stock.quant.strategy.feedback import compute_coefs

    coefs, _ = compute_coefs(
        {"政策": {"judged": 40, "unjudged_rate": 0.0, "win_rate": 1.0},
         "情绪": {"judged": 40, "unjudged_rate": 0.0, "win_rate": 0.0}},
        min_samples=30, lo=0.8, hi=1.2,
    )
    assert coefs == {"政策": 1.2, "情绪": 0.8}
