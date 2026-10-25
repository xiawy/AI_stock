"""大盘择时门回归测试 (FR-08).

修复前的两个问题:

- **whipsaw**: 单指数 + 单 MA20 且无确认、无缓冲 —— 贴着均线震荡时闸门每天反转,
  等于随机开关新建仓。
- **unknown 无补偿**: 数据源挂掉时既不拦截也不收紧, 系统在「不知道大盘方向」的
  状态下照样满仓开新仓。

修复后: risk_on 需连续 2 个交易日且收盘高出 MA20 至少 1%; risk_off 立即生效;
unknown 连续 3 个交易日 → 建仓系数 ×0.5 (只收紧不阻断)。
"""

from datetime import datetime, timedelta, timezone

import pytest


TZ8 = timezone(timedelta(hours=8))
WINDOW = 20


@pytest.fixture()
def regime_env(quant_env, monkeypatch):
    """隔离择时模块的进程内缓存 + 持久化状态 + 时钟."""
    from ai_stock.quant import market_regime

    market_regime.reset_cache()
    market_regime._reset_state_for_tests()

    class _Clock:
        def __init__(self):
            self.now = datetime(2026, 10, 19, 10, 30, tzinfo=TZ8)   # 周一

        def __call__(self):
            return self.now

        def next_day(self):
            self.now = self.now + timedelta(days=1)
            return self.now

    clock = _Clock()
    monkeypatch.setattr(market_regime, "_now", clock)
    yield market_regime, clock
    market_regime.reset_cache()


def _series(last: float, base: float = 100.0, n: int = 30) -> list[float]:
    """构造 n 根日线: 前面全是 base, 最后一根为 ``last``."""
    return [base] * (n - 1) + [last]


def _patch_closes(monkeypatch, closes):
    from ai_stock.quant import market_regime

    monkeypatch.setattr(market_regime, "_fetch_index_closes", lambda window: list(closes))


# ---------------------------------------------------------------------------
# 连续确认 + 缓冲
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConfirmAndBuffer:
    def test_single_day_is_not_enough(self, regime_env, monkeypatch):
        module, clock = regime_env
        _patch_closes(monkeypatch, _series(110.0))

        first = module.get_market_regime(force=True)
        assert first["regime"] == "risk_off"
        assert first["risk_on_streak"] == 1
        assert "未确认" in first["reason"] or "仅确认" in first["reason"]

        clock.next_day()
        second = module.get_market_regime(force=True)
        assert second["regime"] == "risk_on"
        assert second["risk_on_streak"] == 2

    def test_same_day_calls_do_not_advance_streak(self, regime_env, monkeypatch):
        """盘中每 10 分钟调用一次, 不能把「2 个交易日」算成「2 次调用」。"""
        module, _ = regime_env
        _patch_closes(monkeypatch, _series(110.0))

        for _ in range(5):
            out = module.get_market_regime(force=True)

        assert out["risk_on_streak"] == 1
        assert out["regime"] == "risk_off"

    def test_buffer_excludes_hovering_above_ma(self, regime_env, monkeypatch):
        """贴着 MA20 上方 0.3% 震荡: 无论多少天都不算 risk_on。"""
        module, clock = regime_env
        _patch_closes(monkeypatch, _series(100.3))

        for _ in range(5):
            out = module.get_market_regime(force=True)
            clock.next_day()

        assert out["regime"] == "risk_off"

    def test_risk_off_is_immediate(self, regime_env, monkeypatch):
        module, clock = regime_env
        _patch_closes(monkeypatch, _series(110.0))
        module.get_market_regime(force=True)
        clock.next_day()
        assert module.get_market_regime(force=True)["regime"] == "risk_on"

        clock.next_day()
        _patch_closes(monkeypatch, _series(95.0))       # 跌破均线
        out = module.get_market_regime(force=True)
        assert out["regime"] == "risk_off"
        assert out["risk_on_streak"] == 0

    def test_rollback_to_original_criteria(self, regime_env, monkeypatch):
        """market_confirm_days=1 + market_buffer=0 → 回到「一天即 risk_on」。"""
        from ai_stock.quant import db_ops

        module, _ = regime_env
        db_ops.set_config_value("market_confirm_days", "1")
        db_ops.set_config_value("market_buffer", "0")
        _patch_closes(monkeypatch, _series(100.5))

        assert module.get_market_regime(force=True)["regime"] == "risk_on"


# ---------------------------------------------------------------------------
# unknown: 告警 + 仓位收紧
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUnknownRegime:
    def test_unknown_does_not_block_but_counts_days(self, regime_env, monkeypatch):
        module, clock = regime_env
        _patch_closes(monkeypatch, [])

        first = module.get_market_regime(force=True)
        assert first["regime"] == "unknown"
        assert first["ok"] is False
        assert first["unknown_days"] == 1
        assert module.unknown_penalty_coef() == 1.0

        clock.next_day()
        second = module.get_market_regime(force=True)
        assert second["unknown_days"] == 2
        assert module.unknown_penalty_coef() == 1.0

    def test_three_unknown_days_halve_position(self, regime_env, monkeypatch):
        module, clock = regime_env
        _patch_closes(monkeypatch, [])

        for _ in range(3):
            out = module.get_market_regime(force=True)
            clock.next_day()

        assert out["unknown_days"] == 3
        assert module.unknown_penalty_coef() == pytest.approx(0.5)

    def test_alert_after_warn_hours(self, regime_env, monkeypatch):
        from ai_stock.quant import ops_monitor

        module, clock = regime_env
        _patch_closes(monkeypatch, [])

        emitted: list[tuple] = []
        monkeypatch.setattr(
            ops_monitor, "get_alerts",
            lambda: type("_A", (), {"emit": staticmethod(
                lambda level, atype, msg: emitted.append((level, atype, msg))
            )})(),
        )

        module.get_market_regime(force=True)      # 首次 unknown, 未达 24h
        assert emitted == []

        clock.now = clock.now + timedelta(hours=25)
        module.get_market_regime(force=True)

        assert emitted and emitted[0][1] == "market_regime_unknown"

    def test_recovery_resets_unknown_counter(self, regime_env, monkeypatch):
        module, clock = regime_env
        _patch_closes(monkeypatch, [])
        for _ in range(3):
            module.get_market_regime(force=True)
            clock.next_day()
        assert module.unknown_penalty_coef() == pytest.approx(0.5)

        _patch_closes(monkeypatch, _series(95.0))      # 数据恢复 (走弱)
        out = module.get_market_regime(force=True)

        assert out["regime"] == "risk_off"
        assert out["unknown_days"] == 0
        assert module.unknown_penalty_coef() == 1.0

    def test_confirm_state_survives_cache_reset(self, regime_env, monkeypatch):
        """确认进度持久化在库里: 进程重启 (缓存清空) 不应把连续天数清零。"""
        module, clock = regime_env
        _patch_closes(monkeypatch, _series(110.0))
        module.get_market_regime(force=True)

        module.reset_cache()
        clock.next_day()
        out = module.get_market_regime(force=True)

        assert out["regime"] == "risk_on"
        assert out["risk_on_streak"] == 2
