"""交易链路归因报表的回归测试 (FR-14).

报表本身要能回答三个问题, 且必须**只读**:

1. 决策漏斗卡在哪一步 (按 agent/decision 计数);
2. 二次验证的拒绝原因分布 (reject_stage) 与通过率;
3. 已实现盈亏 / 胜率 / 盈亏比 / 卖出原因分布。

"只读"是硬约束: 这个脚本要在生产上随时跑, 一旦它会写配置或触发 LLM,
就不可能被放心地放进 cron。
"""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "trade_attribution.py"


@pytest.fixture(scope="module")
def attribution():
    spec = importlib.util.spec_from_file_location("trade_attribution", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _decision(agent, decision, *, symbol="", detail=None):
    from ai_stock.quant import db_ops

    db_ops.log_decision(agent=agent, decision=decision, symbol=symbol, detail=detail or {})


def _trade(side, *, symbol="600519", status="filled", realized_pnl=0.0, reason=""):
    from ai_stock.quant import db_ops

    db_ops.add_trade_log({
        "symbol": symbol, "side": side, "status": status,
        "price": 100.0, "quantity": 100, "amount": 10_000.0, "fee": 5.0,
        "realized_pnl": realized_pnl, "reason": reason,
    })


# ---------------------------------------------------------------------------
# 漏斗与拒绝原因
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFunnel:
    def test_decision_counts(self, quant_env, attribution):
        _decision("second_verification", "verification_passed")
        _decision("second_verification", "verification_rejected",
                  detail={"reject_stage": "weak_risk_reward"})
        _decision("second_verification", "verification_rejected",
                  detail={"reject_stage": "weak_risk_reward"})
        _decision("reduce_position", "reduce_hold")

        report = attribution.collect(30)
        funnel = {(f["agent"], f["decision"]): f["count"] for f in report["funnel"]}

        assert funnel[("second_verification", "verification_passed")] == 1
        assert funnel[("second_verification", "verification_rejected")] == 2
        assert funnel[("reduce_position", "reduce_hold")] == 1

    def test_reject_stages_and_pass_rate(self, quant_env, attribution):
        for _ in range(3):
            _decision("second_verification", "verification_passed")
        _decision("second_verification", "verification_rejected",
                  detail={"reject_stage": "bear_deteriorated"})
        _decision("second_verification", "verification_rejected",
                  detail={"reject_stage": "rr_missing"})
        # 其它 agent 的记录不参与二次验证漏斗
        _decision("position_open", "open_risk_rejected", detail={"reject_stage": "x"})

        verification = attribution.collect(30)["verification"]

        assert verification["total"] == 5
        assert verification["stages"]["passed"] == 3
        assert verification["stages"]["bear_deteriorated"] == 1
        assert verification["stages"]["rr_missing"] == 1
        assert verification["pass_rate"] == pytest.approx(0.6)

    def test_window_excludes_old_decisions(self, quant_env, attribution):
        from ai_stock.quant.db import session_scope
        from ai_stock.quant.db_models import AgentDecisionLog

        old = datetime.now(timezone.utc) - timedelta(days=40)
        with session_scope() as s:
            s.add(AgentDecisionLog(
                agent="second_verification", decision="verification_passed",
                symbol="600000", ts=old,
            ))
            s.commit()

        assert attribution.collect(30)["decisions"] == 0
        assert attribution.collect(60)["decisions"] == 1


# ---------------------------------------------------------------------------
# 成交质量
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTradeStats:
    def test_pnl_and_win_rate(self, quant_env, attribution):
        _trade("buy")
        _trade("buy")
        _trade("sell", realized_pnl=200.0, reason="硬性止损: 浮亏 5.1%")
        _trade("sell", realized_pnl=100.0, reason="硬性止损: 浮亏 5.1%")
        _trade("sell", realized_pnl=-100.0, reason="触及止盈价 120.0")
        _trade("buy", status="rejected")

        stats = attribution.collect(30)["trades"]

        assert stats["filled_orders"] == 5
        assert stats["buy_orders"] == 2
        assert stats["sell_orders"] == 3
        assert stats["rejected_orders"] == 1
        assert stats["realized_pnl"] == pytest.approx(200.0)
        assert stats["win_count"] == 2
        assert stats["loss_count"] == 1
        assert stats["win_rate"] == pytest.approx(2 / 3, abs=1e-4)
        assert stats["avg_win"] == pytest.approx(150.0)
        assert stats["avg_loss"] == pytest.approx(100.0)
        assert stats["profit_factor"] == pytest.approx(1.5)

    def test_no_loss_samples_yields_none_profit_factor(self, quant_env, attribution):
        _trade("sell", realized_pnl=100.0)
        stats = attribution.collect(30)["trades"]
        assert stats["profit_factor"] is None       # 不写成 inf 骗自己
        assert stats["win_rate"] == pytest.approx(1.0)

    def test_no_sell_samples(self, quant_env, attribution):
        _trade("buy")
        stats = attribution.collect(30)["trades"]
        assert stats["win_rate"] is None
        assert stats["realized_pnl"] == 0.0

    def test_sell_reason_mix(self, quant_env, attribution):
        _trade("sell", realized_pnl=10.0, reason="硬性止损: 浮亏 5.1%")
        _trade("sell", realized_pnl=10.0, reason="硬性止损: 浮亏 5.4%")
        _trade("sell", realized_pnl=10.0, reason="时间止损: 持有 7 个交易日")

        reasons = attribution.collect(30)["sell_reasons"]
        top = {r["reason"]: r["count"] for r in reasons}

        # 浮亏百分比是参数不是分类维度: 5.1% 与 5.4% 必须归并到同一个桶
        assert top["硬性止损: 浮亏"] == 2
        assert top["时间止损: 持有"] == 1
        assert len(reasons) == 2

    def test_reason_bucket_keeps_numeric_only_reason(self, attribution):
        # 以数字开头的原因取不到语义前缀, 不能一律归成"未记录"
        assert attribution._reason_bucket("5% 止损") == "5% 止损"
        assert attribution._reason_bucket("") == "未记录"
        assert attribution._reason_bucket("触及止盈价 120.0") == "触及止盈价"


# ---------------------------------------------------------------------------
# 退出意图清单 + 只读约束 + 文本渲染
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPendingAndReadOnly:
    def test_pending_exit_listed(self, quant_env, attribution):
        from ai_stock.quant import db_ops, exit_state

        db_ops.upsert_holding({
            "symbol": "600519", "name": "贵州茅台", "quantity": 1000,
            "available_quantity": 1000, "cost_price": 60.0,
        })
        exit_state.mark_pending_exit(
            "600519", reason="硬性止损", portion=1.0, error="跌停价 54.0 不可卖出",
        )

        rows = attribution.collect(30)["pending_exit"]

        assert len(rows) == 1
        assert rows[0]["symbol"] == "600519"
        assert rows[0]["fail_count"] == 1
        assert "跌停" in rows[0]["last_error"]

    def test_report_does_not_modify_config(self, quant_env, attribution):
        from ai_stock.quant import db_ops

        db_ops.set_config_value("initial_cash", "1234567")
        before = db_ops.get_all_config()

        _decision("second_verification", "verification_passed")
        _trade("sell", realized_pnl=10.0)
        attribution.collect(30)

        assert db_ops.get_all_config() == before

    def test_render_has_all_sections(self, quant_env, attribution):
        _decision("second_verification", "verification_passed")
        _trade("sell", realized_pnl=10.0, reason="硬性止损")
        text = attribution.render(attribution.collect(30))

        for marker in ("二次验证漏斗", "成交与盈亏", "卖出原因分布", "退出意图"):
            assert marker in text

    def test_json_output_is_serialisable(self, quant_env, attribution):
        _decision("second_verification", "verification_passed")
        payload = json.dumps(attribution.collect(30), ensure_ascii=False, default=str)
        assert "verification" in json.loads(payload)
