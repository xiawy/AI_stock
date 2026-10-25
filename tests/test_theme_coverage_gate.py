"""主题闸门的日线覆盖比例回归测试 (FR-12).

原口径 ``len(hist) < min(5, len(symbols))``: 8 只成分股只要 5 只有历史就放行。
相关性只用 5 条序列 (10 个 pair) 估计, 离散度更是靠 5 个样本的标准差 —— 结论
基本是噪音, 却会被当成"赛道成立"。

改为按比例 (70%) + 绝对下限 (6 只) 要求。数据不足不是"主题不合格", 所以降为
shadow 等待补齐; 成分股数量/关键词这类结构性不合格仍然直接 rejected。
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture()
def theme_env(tmp_path, monkeypatch):
    from ai_stock.quant import db

    db_url = f"sqlite:///{tmp_path / 'theme_coverage.db'}"
    monkeypatch.setenv("QUANT_DB_URL", db_url)
    db.reset_engine(db_url)
    db.init_quant_db()
    yield db_url


def _dates(days: int) -> list[str]:
    from datetime import datetime, timedelta

    start = datetime(2026, 5, 1)
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


def _noisy_history(days: int = 65, amp: float = 0.01):
    import random

    rng = random.Random(7)
    closes = [10.0]
    for _ in range(days - 1):
        closes.append(closes[-1] * (1 + rng.uniform(-amp, amp)))
    return {"closes": closes, "volumes": [2e8] * days, "dates": _dates(days)}


def _seed_theme(name: str, n: int):
    from ai_stock.quant.db import session_scope
    from ai_stock.quant.db_models import QuantTheme, QuantThemeStock

    with session_scope() as s:
        theme = QuantTheme(
            name=name, parent_industry="电子", origin="compose", status="candidate",
            thesis="t", driver_keywords_json=json.dumps(["a", "b", "c"]),
        )
        s.add(theme)
        s.flush()
        for i in range(n):
            s.add(QuantThemeStock(theme_id=theme.id, symbol=f"6000{i:02d}", role="core"))
        s.commit()


def _patch_market(monkeypatch, missing: set[str]):
    from ai_stock.quant import theme_registry as tr

    monkeypatch.setattr(
        tr, "_history",
        lambda s, days=65: None if s in missing else _noisy_history(),
    )
    monkeypatch.setattr(tr, "_caps", lambda syms: {s: 1e10 for s in syms})
    monkeypatch.setattr(
        "ai_stock.dataflows.a_stock._tencent_quote",
        lambda codes: {c: {"name": "测试"} for c in codes},
    )


@pytest.mark.unit
class TestHistoryCoverageGate:
    def test_5_of_8_is_not_enough(self, theme_env, monkeypatch):
        """8 只里 5 只有历史 (62.5%): 旧口径放行, 新口径挡住。"""
        from ai_stock.quant import theme_registry as tr

        _seed_theme("覆盖不足（测试）", 8)
        _patch_market(monkeypatch, missing={"600005", "600006", "600007"})

        result = tr.validate_theme("覆盖不足（测试）")

        assert result["passed"] is False
        assert result["metrics"]["coverage"] == pytest.approx(0.625)
        assert result["metrics"]["coverage_required"] == 6
        assert any("覆盖不足" in r for r in result["reasons"])
        # 数据不足不等于主题不合格 → shadow, 等数据补齐后再校验
        assert result["status"] == "shadow"

    def test_7_of_8_passes(self, theme_env, monkeypatch):
        from ai_stock.quant import theme_registry as tr

        _seed_theme("覆盖达标（测试）", 8)
        _patch_market(monkeypatch, missing={"600007"})

        result = tr.validate_theme("覆盖达标（测试）")

        assert result["metrics"]["coverage"] == pytest.approx(0.875)
        assert result["passed"] is True, result["reasons"]
        assert result["status"] == "active"

    def test_required_scales_with_member_count(self, theme_env, monkeypatch):
        """40 只的主题要求 28 只有历史 (70%), 不能只靠 5 只凑数。"""
        from ai_stock.quant import theme_registry as tr

        _seed_theme("大主题（测试）", 40)
        missing = {f"6000{i:02d}" for i in range(15, 40)}      # 只有 15 只有历史
        _patch_market(monkeypatch, missing=missing)

        result = tr.validate_theme("大主题（测试）")

        assert result["metrics"]["coverage_required"] == 28
        assert result["passed"] is False
        assert result["status"] == "shadow"

    def test_structural_rejection_stays_rejected(self, theme_env, monkeypatch):
        """成分股只有 5 只 (结构性不合格) + 数据不足 → 仍然是 rejected, 不是 shadow。"""
        from ai_stock.quant import theme_registry as tr

        _seed_theme("小主题（测试）", 5)
        _patch_market(monkeypatch, missing=set())

        result = tr.validate_theme("小主题（测试）")

        assert result["passed"] is False
        assert result["status"] == "rejected"
        assert any("成分股" in r for r in result["reasons"])

    def test_full_coverage_keeps_active(self, theme_env, monkeypatch):
        from ai_stock.quant import theme_registry as tr

        _seed_theme("全覆盖（测试）", 10)
        _patch_market(monkeypatch, missing=set())

        result = tr.validate_theme("全覆盖（测试）")

        assert result["metrics"]["coverage"] == pytest.approx(1.0)
        assert result["status"] == "active"
