"""端到端回放: 用 ReplayAdapter 跑完整流程, 验证"缺数据也能出报告".

这条用例守的是产品底线: 任一数据源挂掉, 报告仍要生成并落库, 且用户能看到
哪一块是缺的 —— 空报告比没有记录好, 因为没有记录会让记分体系出现空洞.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    """单测不出网: 裁决器走规则版 (它永远可用)."""
    monkeypatch.setenv("STRATEGY_DESK_LLM", "0")


@pytest.fixture()
def quant_db(tmp_path):
    """独立的临时 SQLite, 不污染开发库."""
    from ai_stock.quant import db

    url = f"sqlite:///{(tmp_path / 'strategy.db').as_posix()}"
    db.reset_engine(url)
    db.init_quant_db()
    yield url


def _snapshot() -> dict:
    """一份"有涨停、有资金、无宏观"的快照: 数据部分缺失的典型场景."""
    return {
        "board_flow": [
            {"code": "BK1033", "name": "CPO", "change_pct": 6.2, "main_net_inflow": 1.2e9},
            {"code": "BK0478", "name": "锂矿", "change_pct": 3.1, "main_net_inflow": 3.0e8},
        ],
        "constituents": {
            "BK1033": [
                {"code": "300308", "name": "中际旭创", "change_pct": 9.9, "amount": 4e9},
                {"code": "300502", "name": "新易盛", "change_pct": 6.1, "amount": 3e9},
                {"code": "002281", "name": "光迅科技", "change_pct": 4.2, "amount": 1e9},
                {"code": "300394", "name": "天孚通信", "change_pct": 3.3, "amount": 9e8},
                {"code": "600487", "name": "亨通光电", "change_pct": 2.8, "amount": 8e8},
            ],
            "BK0478": [
                {"code": "002466", "name": "天齐锂业", "change_pct": 5.5, "amount": 2e9},
                {"code": "002460", "name": "赣锋锂业", "change_pct": 4.1, "amount": 2e9},
            ],
        },
        "concepts": {
            "300308": ["CPO", "光模块"], "300502": ["CPO"], "002281": ["CPO"],
            "300394": ["CPO"], "600487": ["CPO"],
            "002466": ["盐湖提锂"], "002460": ["盐湖提锂"],
        },
        "limit_up": [
            {"code": "300308", "name": "中际旭创", "trade_date": "2026-10-20", "reason_tags": ["CPO"]},
            {"code": "300308", "name": "中际旭创", "trade_date": "2026-10-21", "reason_tags": ["CPO"]},
            {"code": "300308", "name": "中际旭创", "trade_date": "2026-10-22", "reason_tags": ["CPO"]},
            {"code": "002466", "name": "天齐锂业", "trade_date": "2026-10-22", "reason_tags": ["盐湖提锂"]},
        ],
        "fund_flow": {
            "300308": [-1e4, -2e4, -3e4],       # 连板但 D0 净流出 → 边拉边撤
            "300502": [1e4, 2e4, 3e4],
            "002281": [1e3, 1e3, 1e3],
            "300394": [1e3, 1e3, 1e3],
            "600487": [1e3, 1e3, 1e3],
        },
        "macro": {},                              # 宏观全缺 → regime 未定
        # 新闻榜的主题级方向 (不是标题命中): CPO 利多, 锂矿利空
        "news_impacts": [
            {"theme": "CPO", "direction": "bullish", "strength": 3, "horizon": "3d",
             "base_date": "2026-10-22", "exp_low": 3.0, "exp_high": 6.0},
            {"theme": "锂矿", "direction": "bearish", "strength": 2, "horizon": "1d",
             "base_date": "2026-10-22"},
        ],
        # 龙虎榜: 中际旭创有机构专用席位净买入
        "institutional": {"300308": True},
    }


def test_full_flow_with_missing_macro(quant_db):
    from ai_stock.quant.strategy.adapters import ReplayAdapter
    from ai_stock.quant.strategy.repo import StrategyRepo
    from ai_stock.quant.strategy.service import run_strategy_flow

    result = run_strategy_flow("2026-10-22", port=ReplayAdapter(_snapshot()))

    assert result["ok"] is True
    # 宏观全缺 → 未定, 但流程不能因此中断
    assert result["regime"] == "未定"
    assert result["boards"] >= 1
    assert result["claims"] >= 1

    report = StrategyRepo().load_report("2026-10-22", "close")
    assert report is not None
    sections = report["sections"]
    assert sections["titles"] == ["大盘总览", "关注板块", "交易建议"]
    assert len(sections["overview"]) == 7
    assert sections["notice"] == "收盘定稿"
    # 新闻面必须真的参与: 命中 2 条主题证据, 否则矩阵只是两方对齐
    assert sections["news_hits"] == 2

    # CPO 5 只只有 1 只有归因 → 必须是"观察/个股事件", 不许是板块级
    cpo = [b for b in sections["boards"] if "CPO" in b["name"]]
    assert cpo, json.dumps(sections["boards"], ensure_ascii=False)
    assert all(b["scope"] != "板块级" or b["heat"].startswith(("强", "中")) for b in cpo)
    assert cpo[0]["heat"].startswith(("观察", "无", "强", "中"))
    # 催化窗口来自新闻榜 horizon/预期区间, 不能是占位符
    assert cpo[0]["calendar"] == "T+3 内 · 预期 +3.0~+6.0%"
    assert "中际旭创" in cpo[0]["leaders"]

    # 连板 3 天的高标必须被溯源
    assert any(t["boards"] == 3 for t in sections["leaders"])

    # 三方交叉验证: CPO 结构/资金/新闻同向, 但新闻主题与涨停归因同名 →
    # 必须标伪共振 (它只是把归因复述了一遍), 不得当成三条独立证据
    cpo_row = next(r for r in sections["matrix"] if r["basket_name"] == "CPO")
    assert cpo_row["consistency"].startswith("✅")
    assert cpo_row["pseudo_resonance"] is True
    # 锂矿: 结构 +1 / 新闻 -1 → 必须判冲突, 不许因为"看起来涨了"就算共振
    li_row = next(r for r in sections["matrix"] if "锂矿" in r["basket_name"])
    assert li_row["consistency"].startswith("❌")


def test_report_is_idempotent_but_claims_are_not_duplicated(quant_db):
    """重跑覆盖报告, 但断言不重复记账 (否则胜率会被稀释)."""
    from ai_stock.quant.strategy.adapters import ReplayAdapter
    from ai_stock.quant.strategy.repo import StrategyRepo
    from ai_stock.quant.strategy.service import run_strategy_flow

    first = run_strategy_flow("2026-10-22", port=ReplayAdapter(_snapshot()))
    second = run_strategy_flow("2026-10-22", port=ReplayAdapter(_snapshot()))

    assert first["claims"] == second["claims"]      # 返回的是"尝试写入条数"
    score = StrategyRepo().score()
    assert score["total"] == first["claims"]        # 实际只入库一份


def test_coef_is_stored_but_not_consumed_until_enabled(quant_db):
    """系数照算照存, 但开关关闭时选股侧拿到的乘子必须为空.

    "算"与"用"分开是刻意的: 先观察两周系数是否稳定, 再决定是否让它影响排序.
    """
    from ai_stock.quant import db_ops
    from ai_stock.quant.strategy import feedback
    from ai_stock.quant.strategy.adapters import ReplayAdapter
    from ai_stock.quant.strategy.service import run_strategy_flow, update_coefs

    run_strategy_flow("2026-10-22", port=ReplayAdapter(_snapshot()))

    # 造 31 条"情绪"驱动的已判定断言: 30 条兑现 + 1 条证伪 → 胜率 0.968
    from datetime import date

    from ai_stock.quant.strategy.contracts import Claim, Judgement, Regime
    from ai_stock.quant.strategy.repo import StrategyRepo

    repo = StrategyRepo()
    claims = [
        Claim(
            claim_id=f"T-M{i}", period="2026-10-21-P1", type="MAINLINE",
            target="CPO", driver="情绪", direction="上行",
            verify_date=date(2026, 10, 22),
        )
        for i in range(31)
    ]
    repo.save_run(
        trade_date="2026-10-21", snapshot_type="close", effective_from="",
        evidences=[], verdicts=[], traces=[], shadows=[],
        regime=Regime(), claims=claims, sections={}, degraded=[], prompt_version=1,
    )

    for i in range(31):
        repo.apply_judgement(
            f"T-M{i}", Judgement(
                state="judged", outcome="兑现" if i < 30 else "证伪", move=1.5,
            ),
        )

    res = update_coefs("2026-10-22")
    expected = round(0.8 + 0.4 * (30 / 31), 3)      # 胜率 30/31 线性映射到 0.8~1.2
    assert res["coefs"] == {"情绪": expected}
    assert db_ops.get_config_value(feedback.CONFIG_KEY)        # 已落库

    # 开关关闭 → 不调权
    db_ops.set_config_value(feedback.ENABLE_KEY, "0")
    assert feedback.multipliers(["CPO"], "2026-10-22") == {}

    # 开关打开 → 篮子名与行业名可匹配, 乘子生效
    db_ops.set_config_value(feedback.ENABLE_KEY, "1")
    assert feedback.multipliers(["CPO"], "2026-10-22") == {"CPO": expected}

    # 匹配不上的行业一律不调 —— 猜一个 driver 比不调更糟
    assert feedback.multipliers(["猪肉"], "2026-10-22") == {}
