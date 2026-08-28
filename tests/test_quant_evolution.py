"""自我进化模块测试 (设计文档 §10.3 反过拟合保护).

覆盖: 严格时序分割 / 止损策略回测 / 训练-验证-样本外三段校验 /
样本外不达标直接丢弃 / 达标候选规则永不自动启用 (仅进人工审核).
全部使用注入的合成价格序列, 不依赖外部行情.
"""

from datetime import datetime, timezone

import pytest


@pytest.fixture()
def quant_env(tmp_path, monkeypatch):
    from ai_stock.quant import db, mq

    db_url = f"sqlite:///{tmp_path / 'quant_evo.db'}"
    monkeypatch.setenv("QUANT_DB_URL", db_url)
    monkeypatch.setattr(mq, "MQ_BACKEND", "sqlite")
    db.reset_engine(db_url)
    mq.reset_mq_backend()
    from ai_stock.quant.db import init_quant_db

    init_quant_db()
    yield db_url
    mq.reset_mq_backend()


def _uptrend(n: int = 120, start: float = 10.0, step: float = 0.10) -> list[float]:
    return [round(start + i * step, 4) for i in range(n)]


def _downtrend(n: int = 120, start: float = 20.0, step: float = 0.40) -> list[float]:
    return [round(max(start - i * step, 0.5), 4) for i in range(n)]


@pytest.mark.unit
class TestTimeSeriesSplit:
    def test_split_is_contiguous_and_ordered(self):
        from ai_stock.quant.evolution import time_series_split

        train, valid, oos = time_series_split(100)
        # 三段连续、不打乱、无重叠 (§10.3)
        assert list(train) == list(range(0, 60))
        assert list(valid) == list(range(60, 80))
        assert list(oos) == list(range(80, 100))
        assert train.stop == valid.start and valid.stop == oos.start


@pytest.mark.unit
class TestBacktestStopPolicy:
    def test_stop_loss_triggers_on_downtrend(self):
        from ai_stock.quant.evolution import backtest_stop_policy

        result = backtest_stop_policy(_downtrend(), stop_loss_pct=0.05)
        assert result["exit_reason"] == "stop_loss"
        assert result["total_return"] <= -0.04

    def test_time_stop_triggers_on_flat(self):
        from ai_stock.quant.evolution import backtest_stop_policy

        flat = [10.0] * 40
        result = backtest_stop_policy(flat, stop_loss_pct=0.05, time_stop_days=5)
        assert result["exit_reason"] == "time_stop"
        assert result["holding_bars"] == 5

    def test_uptrend_holds_to_end(self):
        from ai_stock.quant.evolution import backtest_stop_policy

        result = backtest_stop_policy(_uptrend(), stop_loss_pct=0.05)
        assert result["exit_reason"] == "hold_to_end"
        assert result["total_return"] > 0


@pytest.mark.unit
class TestEvolveStopLoss:
    def test_skipped_without_enough_data(self, quant_env):
        from ai_stock.quant.evolution import evolve_stop_loss

        outcome = evolve_stop_loss(closes_by_symbol={"600000": [10.0] * 20})
        assert outcome["status"] == "skipped"

    def test_oos_rejection_on_downtrend(self, quant_env):
        """样本外绩效不达标 → 直接丢弃并写入 evolution_history."""
        from ai_stock.quant import db_ops
        from ai_stock.quant.evolution import evolve_stop_loss

        series = {f"s{i}": _downtrend() for i in range(3)}
        outcome = evolve_stop_loss(closes_by_symbol=series)
        assert outcome["status"] == "oos_rejected"
        records = db_ops.get_evolution_records(status="oos_rejected")
        assert records, "三套数据集指标必须写入 evolution_history"
        assert records[0]["oos_metrics"], records[0]

    def test_candidate_never_auto_enabled_on_pass(self, quant_env):
        """达标候选: 进入审核队列且永远 enabled=False (禁止自动上线)."""
        from ai_stock.quant import db_ops
        from ai_stock.quant.evolution import evolve_stop_loss

        series = {f"s{i}": _uptrend(step=0.10 + i * 0.002) for i in range(4)}
        outcome = evolve_stop_loss(closes_by_symbol=series)
        # simpleeval 缺失环境下测试用例走白名单降级 → test_failed 也属安全拦截;
        # 两种结果都不允许规则自动启用
        assert outcome["status"] in ("pending_review", "test_failed")
        assert outcome["metrics"]["oos"]["max_drawdown"] <= 0.25
        if outcome["status"] == "pending_review":
            rules = {
                r["rule_id"]: r
                for r in db_ops.get_rules()
            }
            assert outcome["rule_id"] in rules
            assert rules[outcome["rule_id"]]["enabled"] is False
            assert rules[outcome["rule_id"]]["gray_scale"] is True

    def test_grid_search_only_on_train(self, quant_env):
        """调参只跑训练集: 验证集指标仅在最优参数上计算一次."""
        from ai_stock.quant.evolution import (
            DEFAULT_GRID,
            search_params,
            time_series_split,
        )

        series = {"s0": _uptrend(), "s1": _uptrend(step=0.12)}
        n = min(len(v) for v in series.values())
        train_seg, valid_seg, _ = time_series_split(n)
        params, metrics = search_params(series, train_seg, valid_seg)
        assert params in DEFAULT_GRID
        assert metrics["train"]["samples"] > 0
        assert metrics["valid"]["samples"] > 0
