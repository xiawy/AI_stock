"""L3 细分赛道注册表：种子幂等、6 道校验、自动流转、新闻匹配。

用 mock 行情验证"闸门真的会拦"——校验逻辑本身不能依赖网络，
否则线上跑一次失败就无法区分"主题不合格"和"数据源抖动"。
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def theme_env(tmp_path, monkeypatch):
    from ai_stock.quant import db

    db_url = f"sqlite:///{tmp_path / 'theme_test.db'}"
    monkeypatch.setenv("QUANT_DB_URL", db_url)
    db.reset_engine(db_url)
    db.init_quant_db()
    yield db_url


def _dates(days: int) -> list[str]:
    from datetime import datetime, timedelta

    start = datetime(2026, 5, 1)
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


def _flat_history(days: int = 65, drift: float = 0.0):
    """构造一条平稳日线（收盘价 + 成交量 + 日期）。"""
    closes = [10.0]
    for _ in range(days - 1):
        closes.append(closes[-1] * (1 + drift))
    return {
        "closes": closes,
        "volumes": [1e8] * days,
        "dates": _dates(days),
    }


def _noisy_history(seed: int = 1, days: int = 65, amp: float = 0.01):
    import random

    rng = random.Random(seed)
    closes = [10.0]
    for _ in range(days - 1):
        closes.append(closes[-1] * (1 + rng.uniform(-amp, amp)))
    return {
        "closes": closes,
        "volumes": [2e8] * days,
        "dates": _dates(days),
    }


def _seed_one(name: str, n: int, *, keywords=("a", "b", "c"), aliases=("测试别名",)):
    """写一个主题（不走默认种子，便于构造边界用例）。"""
    import json

    from ai_stock.quant.db import session_scope
    from ai_stock.quant.db_models import QuantTheme, QuantThemeAlias, QuantThemeStock

    with session_scope() as s:
        theme = QuantTheme(
            name=name, parent_industry="电子", origin="compose", status="candidate",
            thesis="t", driver_keywords_json=json.dumps(list(keywords)),
        )
        s.add(theme)
        s.flush()
        for i in range(n):
            s.add(QuantThemeStock(theme_id=theme.id, symbol=f"6000{i:02d}", role="core"))
        for a in aliases:
            s.add(QuantThemeAlias(alias=a, theme_id=theme.id))
        s.commit()
        return theme.id


class TestSeedAndGates:
    def test_seed_is_idempotent(self, theme_env):
        from ai_stock.quant import theme_registry as tr
        from ai_stock.quant.theme_seed import DEFAULT_THEMES

        total = len(DEFAULT_THEMES)
        first = tr.seed_default_themes()
        assert first["created"] == total
        second = tr.seed_default_themes()
        assert second["created"] == 0
        assert second["skipped"] == total

    def test_rejects_too_few_members(self, theme_env, monkeypatch):
        from ai_stock.quant import theme_registry as tr

        _seed_one("小主题（测试）", 5)
        monkeypatch.setattr(tr, "_history", lambda s, days=65: _flat_history())
        monkeypatch.setattr(tr, "_caps", lambda syms: {s: 1e10 for s in syms})
        monkeypatch.setattr(
            "ai_stock.dataflows.a_stock._tencent_quote",
            lambda codes: {c: {"name": "测试"} for c in codes},
        )

        result = tr.validate_theme("小主题（测试）")
        assert result["passed"] is False
        assert any("成分股" in r for r in result["reasons"])

    def test_rejects_concentrated_theme(self, theme_env, monkeypatch):
        from ai_stock.quant import theme_registry as tr

        _seed_one("集中主题（测试）", 10)
        monkeypatch.setattr(tr, "_history", lambda s, days=65: _flat_history())
        # 一只票占 90% 市值
        monkeypatch.setattr(
            tr, "_caps",
            lambda syms: {s: (9e11 if s == "600000" else 1e9) for s in syms},
        )
        monkeypatch.setattr(
            "ai_stock.dataflows.a_stock._tencent_quote",
            lambda codes: {c: {"name": "测试"} for c in codes},
        )

        result = tr.validate_theme("集中主题（测试）")
        assert result["passed"] is False
        assert any("单票权重" in r or "前两大" in r for r in result["reasons"])

    def test_rejects_illiquid_theme(self, theme_env, monkeypatch):
        from ai_stock.quant import theme_registry as tr

        _seed_one("冷门主题（测试）", 10)
        illiquid = _flat_history()
        illiquid["volumes"] = [1e4] * len(illiquid["closes"])   # 成交额≈10万
        monkeypatch.setattr(tr, "_history", lambda s, days=65: illiquid)
        monkeypatch.setattr(tr, "_caps", lambda syms: {s: 1e10 for s in syms})
        monkeypatch.setattr(
            "ai_stock.dataflows.a_stock._tencent_quote",
            lambda codes: {c: {"name": "测试"} for c in codes},
        )

        result = tr.validate_theme("冷门主题（测试）")
        assert result["passed"] is False
        assert any("成交额" in r for r in result["reasons"])

    def test_good_theme_becomes_active(self, theme_env, monkeypatch):
        from ai_stock.quant import theme_registry as tr

        _seed_one("合格主题（测试）", 10)
        monkeypatch.setattr(tr, "_history", lambda s, days=65: _noisy_history())
        monkeypatch.setattr(tr, "_caps", lambda syms: {s: 1e10 for s in syms})
        monkeypatch.setattr(
            "ai_stock.dataflows.a_stock._tencent_quote",
            lambda codes: {c: {"name": "测试"} for c in codes},
        )

        result = tr.validate_theme("合格主题（测试）")
        assert result["passed"] is True, result["reasons"]
        assert result["status"] == "active"
        # 校验通过会把日收益回填，供幅度校准用
        assert tr.greeks("合格主题（测试）")["sigma20"] > 0

    def test_short_history_goes_to_shadow(self, theme_env, monkeypatch):
        from ai_stock.quant import theme_registry as tr

        _seed_one("新股主题（测试）", 10)
        short = _noisy_history(days=30)
        monkeypatch.setattr(tr, "_history", lambda s, days=65: short)
        monkeypatch.setattr(tr, "_caps", lambda syms: {s: 1e10 for s in syms})
        monkeypatch.setattr(
            "ai_stock.dataflows.a_stock._tencent_quote",
            lambda codes: {c: {"name": "测试"} for c in codes},
        )

        result = tr.validate_theme("新股主题（测试）")
        assert result["status"] in ("shadow", "rejected")
        if result["passed"]:
            assert result["status"] == "shadow"


class TestMatchingAndReview:
    def test_match_only_active_themes(self, theme_env, monkeypatch):
        from ai_stock.quant import theme_registry as tr

        _seed_one("风电（出海）", 10, keywords=("海风", "塔筒", "桩基"), aliases=("海风",))
        # 未校验 → still candidate → 不该被匹配到
        assert tr.match_themes("海风订单超预期") == []

        monkeypatch.setattr(tr, "_history", lambda s, days=65: _noisy_history())
        monkeypatch.setattr(tr, "_caps", lambda syms: {s: 1e10 for s in syms})
        monkeypatch.setattr(
            "ai_stock.dataflows.a_stock._tencent_quote",
            lambda codes: {c: {"name": "测试"} for c in codes},
        )
        tr.validate_theme("风电（出海）")

        hits = tr.match_themes("海风订单超预期，塔筒涨价")
        assert hits and hits[0]["name"] == "风电（出海）"

    def test_auto_review_retires_only_after_two_failures(self, theme_env, monkeypatch):
        """单次不达标只记 streak，连续两次才退休 —— 一次停牌不该杀掉赛道。"""
        from ai_stock.quant import theme_registry as tr

        _seed_one("会退化的主题（测试）", 10)
        monkeypatch.setattr(tr, "_caps", lambda syms: {s: 1e10 for s in syms})
        monkeypatch.setattr(
            "ai_stock.dataflows.a_stock._tencent_quote",
            lambda codes: {c: {"name": "测试"} for c in codes},
        )
        monkeypatch.setattr(tr, "_history", lambda s, days=65: _noisy_history())
        assert tr.validate_theme("会退化的主题（测试）")["status"] == "active"

        illiquid = _flat_history()
        illiquid["volumes"] = [1e4] * len(illiquid["closes"])
        monkeypatch.setattr(tr, "_history", lambda s, days=65: illiquid)

        first = tr.auto_review()
        assert first["retired"] == []
        assert any(x["name"] == "会退化的主题（测试）" for x in first["warned"])
        assert tr.get_theme("会退化的主题（测试）")["status"] == "active"
        assert tr.get_theme("会退化的主题（测试）")["fail_streak"] == 1

        second = tr.auto_review()
        assert any(x["name"] == "会退化的主题（测试）" for x in second["retired"])
        assert tr.get_theme("会退化的主题（测试）")["status"] == "retired"

    def test_auto_review_aborts_when_data_source_is_down(self, theme_env, monkeypatch):
        """全部主题"拿不到历史" = 数据源故障，不能当成"全部不合格"。"""
        from ai_stock.quant import theme_registry as tr

        for i in range(3):
            _seed_one(
                "数据故障主题%d（测试）" % i, 10,
                aliases=("测试别名%d" % i,),
            )
        monkeypatch.setattr(tr, "_caps", lambda syms: {s: 1e10 for s in syms})
        monkeypatch.setattr(
            "ai_stock.dataflows.a_stock._tencent_quote",
            lambda codes: {c: {"name": "测试"} for c in codes},
        )
        monkeypatch.setattr(tr, "_history", lambda s, days=65: _noisy_history())
        for i in range(3):
            tr.validate_theme("数据故障主题%d（测试）" % i)

        # 熔断/限流：所有票都取不到日线
        monkeypatch.setattr(tr, "_history", lambda s, days=65: None)

        review = tr.auto_review()
        assert review["aborted"], "数据源故障必须整轮放弃"
        assert review["retired"] == []
        for i in range(3):
            theme = tr.get_theme("数据故障主题%d（测试）" % i)
            assert theme["status"] == "active", "不得因为取不到数据而退休"
            assert theme["fail_streak"] == 0, "取不到数据不该计入 streak"
