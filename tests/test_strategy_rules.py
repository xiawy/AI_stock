"""策略台纯函数规则测试.

四个反例是回归基线: 它们是团队连续复发三次的系统性错误 ——
"1 只有证据 + 4 只猜的" 被打包成板块. 这些用例必须永远绿.
"""

from __future__ import annotations

from datetime import date

import pytest

from ai_stock.quant.strategy.contracts import (
    BasketDraft,
    Claim,
    Constituent,
    LimitUpRow,
    MacroVars,
    PriceBar,
)
from ai_stock.quant.strategy.rules import basket as basket_rules
from ai_stock.quant.strategy.rules import claim as claim_rules
from ai_stock.quant.strategy.rules import flow as flow_rules
from ai_stock.quant.strategy.rules import leader as leader_rules
from ai_stock.quant.strategy.rules import matrix as matrix_rules
from ai_stock.quant.strategy.rules import regime as regime_rules


# ---- 工具 ----------------------------------------------------------------


def _members(codes: list[str], pct: float = 5.0) -> list[Constituent]:
    return [
        Constituent(code=c, name=f"S{c}", change_pct=pct, amount=1e8)
        for c in codes
    ]


def _concepts(codes: list[str], concept: str) -> dict[str, list[str]]:
    return {c: [concept] for c in codes}


# ---- M1: 篮子有效性 ------------------------------------------------------


def test_cpo_single_evidence_is_stock_event():
    """CPO 反例: 5 只票只有 1 只有归因 → 只能是个股事件."""
    codes = ["300308", "300502", "002281", "600487", "300394"]
    drafts = basket_rules.build_drafts(
        board=_board("CPO"),
        members=_members(codes),
        concepts_by_symbol=_concepts(codes, "CPO"),
        tags_by_symbol={"300308": ["CPO", "光模块"]},
    )
    v = basket_rules.synthesize(drafts[0])

    assert v.valid_n == 1
    assert v.verdict == "个股事件"
    assert v.rating_cap == "无"


def test_lithium_two_evidence_is_observe_only():
    """锂矿反例: 2 只有证据 → 观察, 不得给板块级."""
    codes = ["002466", "002460", "300014", "002738"]
    drafts = basket_rules.build_drafts(
        board=_board("锂矿"),
        members=_members(codes),
        concepts_by_symbol=_concepts(codes, "盐湖提锂"),
        tags_by_symbol={"002466": ["盐湖提锂"], "002460": ["盐湖提锂"]},
    )
    v = basket_rules.synthesize(drafts[0])

    assert v.valid_n == 2
    assert v.verdict == "观察"
    assert v.rating_cap == "观察"


def test_shipping_three_evidence_never_board_level():
    """航运反例: 3 只有证据仍不够板块级 (门槛 5)."""
    codes = ["601919", "600026", "601872", "601000"]
    drafts = basket_rules.build_drafts(
        board=_board("航运"),
        members=_members(codes),
        concepts_by_symbol=_concepts(codes, "集运"),
        tags_by_symbol={c: ["集运"] for c in codes[:3]},
    )
    v = basket_rules.synthesize(drafts[0])

    assert v.valid_n == 3
    assert v.verdict == "观察"
    assert "禁止包装成板块" not in v.reason  # 2-4 只写"观察: N 只个股的逻辑"


def test_eight_evidence_reaches_strong():
    codes = [f"60{i:04d}" for i in range(9)]
    drafts = basket_rules.build_drafts(
        board=_board("半导体"),
        members=_members(codes),
        concepts_by_symbol=_concepts(codes, "先进封装"),
        tags_by_symbol={c: ["先进封装"] for c in codes},
    )
    v = basket_rules.synthesize(drafts[0])

    assert v.valid_n == 9
    assert v.rating_cap == "强"
    assert v.verdict == "板块级"


def test_all_dropped_never_board_level():
    """家电反例: 概念拆不出来 → 粗口径兜底, 且必须降级."""
    codes = ["000333", "000651", "600690"]
    drafts = basket_rules.build_drafts(
        board=_board("家电"),
        members=_members(codes),
        concepts_by_symbol={},          # 无概念归属
        tags_by_symbol={},
    )
    v = basket_rules.synthesize(drafts[0])

    assert drafts[0].name.endswith("(粗口径)")
    assert v.verdict in ("个股事件", "观察")
    assert v.rating_cap != "强"


def test_merger_driver_is_excluded_from_synthesis():
    """并购重组类异源标的必须剔除, 不参与合成涨跌幅."""
    draft = BasketDraft(
        parent_board="算力",
        name="算力·液冷",
        members=["600001", "600002", "600003"],
        change_pct={"600001": 10.0, "600002": 2.0, "600003": 9.9},
        driver={"600001": "产业", "600002": "产业", "600003": "并购重组"},
        evidence_level={"600001": "C", "600002": "C", "600003": "C"},
    )
    v = basket_rules.synthesize(draft)

    assert "600003" not in v.basket.members or v.synth_change_pct == pytest.approx(6.0)
    assert v.synth_change_pct == pytest.approx(6.0)   # (10+2)/2, 不含 9.9
    assert "剔除" in v.reason


# ---- M2: 高标溯源 --------------------------------------------------------


def test_merger_leader_not_extrapolable():
    attribution, ok = leader_rules.classify("并购重组", has_public_catalyst=True)
    assert attribution == "独立事件"
    assert ok is False


def test_leader_without_catalyst_not_extrapolable():
    attribution, ok = leader_rules.classify("情绪", has_public_catalyst=False)
    assert attribution == "无公开催化"
    assert ok is False


def test_fund_verdict_outflow_beats_seat_info():
    """连板但 D0 净流出 = 出货型, 席位再"机构"也不能叫机构承接."""
    assert leader_rules.judge_fund(-1.0, 5.0, institutional_seats=True) == "边拉边撤"


def test_count_boards_dedups_same_day():
    rows = [
        LimitUpRow(code="000001", name="A", trade_date="2026-10-20", reason_tags=["银行"]),
        LimitUpRow(code="000001", name="A", trade_date="2026-10-20", reason_tags=["银行"]),
        LimitUpRow(code="000001", name="A", trade_date="2026-10-21", reason_tags=["银行"]),
    ]
    assert leader_rules.count_boards(rows) == {"000001": 2}


def test_ladder_health_single_is_fragile():
    traces = leader_rules.build_traces([
        LimitUpRow(code="1", name="A", trade_date=d, reason_tags=["算力"])
        for d in ("2026-10-20", "2026-10-21", "2026-10-22")
    ])
    assert leader_rules.ladder_health(traces) == "仅孤标"


# ---- M4: 宏观闸门 --------------------------------------------------------


def test_missing_macro_never_constrains():
    regime = regime_rules.decide(MacroVars())
    assert regime.code == "未定"
    assert regime.growth_cap is None          # 语义: 不约束, 不是"不利"
    assert regime_rules.apply_cap("强", regime) == "强"


def test_high_vix_caps_growth():
    regime = regime_rules.decide(MacroVars(us10y=4.0, dxy=105, usdcnh=7.2, dr007=1.8, vix=35))
    assert regime.code == "R6"
    assert regime_rules.apply_cap("强", regime) == "中"
    assert regime_rules.apply_cap("观察", regime) == "观察"   # 只收紧, 不上调


# ---- M3: 资金形态 --------------------------------------------------------


@pytest.mark.parametrize("d2,d1,d0,expected", [
    (1.0, 2.0, 3.0, "加速流入"),
    (3.0, 2.0, 1.0, "流入放缓"),
    (-1.0, -2.0, -3.0, "加速流出"),
    (-3.0, -2.0, -1.0, "流出放缓"),
    (-1.0, 1.0, 2.0, "加速流入"),   # D-1 起已转正, 按单调性归"加速"
    (1.0, -1.0, -2.0, "加速流出"),
    (-2.0, 1.0, -3.0, "转流出"),      # 中间一日流入后重新流出
])
def test_flow_pattern(d2, d1, d0, expected):
    assert flow_rules.pattern(d2, d1, d0) == expected


def test_flow_aggregate_needs_all_members():
    series = flow_rules.aggregate("X", ["a", "b"], {"a": [1, 2, 3]})
    assert series.pattern == "数据缺失"


# ---- M5: 交叉验证 --------------------------------------------------------


def test_pseudo_resonance_downgraded():
    draft = BasketDraft(parent_board="X", name="X·Y", members=["a"], change_pct={"a": 5.0})
    v = basket_rules.synthesize(draft)
    m = matrix_rules.build_matrix(
        [v], flow_sign={"X·Y": 1}, news_sign={"X·Y": 1},
        sources={"X·Y": {"同花顺"}},          # 单一来源
    )
    assert m.rows[0].consistency.startswith("✅")
    assert m.rows[0].pseudo_resonance is True


def test_news_without_direction_is_not_resonance():
    draft = BasketDraft(parent_board="X", name="X·Y", members=["a"], change_pct={"a": 5.0})
    v = basket_rules.synthesize(draft)
    m = matrix_rules.build_matrix([v], flow_sign={"X·Y": 1}, news_sign={"X·Y": 0})
    assert not m.rows[0].consistency.startswith("✅")


# ---- M6: 断言四态 --------------------------------------------------------


def _claim(direction="上行", verify="2026-10-27") -> Claim:
    return Claim(
        claim_id="P1-M1", period="2026-10-24-P1", type="MAINLINE",
        target="X·Y", direction=direction, verify_date=date.fromisoformat(verify),
    )


def test_judge_pending_before_verify_date():
    j = claim_rules.judge(_claim(), [], date(2026, 10, 26))
    assert j.state == "pending"


def test_judge_unjudged_when_bar_missing_after_date():
    j = claim_rules.judge(_claim(), [], date(2026, 10, 28))
    assert j.state == "unjudged"
    assert j.reason


def test_judge_provisional_bar_stays_pending():
    bars = [PriceBar(bar_date="2026-10-27", close=10.0, pct_change=3.0, provisional=True)]
    j = claim_rules.judge(_claim(), bars, date(2026, 10, 27))
    assert j.state == "pending"


def test_judge_hit_and_miss():
    up = [PriceBar(bar_date="2026-10-27", close=10.0, pct_change=3.0)]
    assert claim_rules.judge(_claim("上行"), up, date(2026, 10, 27)).outcome == "兑现"
    assert claim_rules.judge(_claim("下行"), up, date(2026, 10, 27)).outcome == "证伪"


def test_judge_flat_direction_uses_band():
    bars = [PriceBar(bar_date="2026-10-27", close=10.0, pct_change=1.0)]
    assert claim_rules.judge(_claim("震荡"), bars, date(2026, 10, 27)).outcome == "兑现"


# ---- 辅助 ----------------------------------------------------------------


def _board(name: str):
    from ai_stock.quant.strategy.contracts import BoardFlow

    return BoardFlow(code="BK0001", name=name, change_pct=4.0, main_net_inflow=1e8)
