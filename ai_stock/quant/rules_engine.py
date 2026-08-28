"""Rule engine + self-evolution pipeline (设计文档 §10).

- 高频硬性风控规则: 预编译为内存 Python 函数 (性能优先)
- 普通策略条件: simpleeval 执行配置化表达式 (strategy_rules 表); 未安装
  simpleeval 时降级为「已知条件白名单」精确匹配, 未知表达式记警告并返回
  False (fail-safe, 不会因降级误触发交易动作)
- 规则字段: version / test_case_ids / gray_scale / min_sample_out_perf
- 规则上线强制流水线 (§10.2): 单测 → 样本外回测 → 人工审核 → 灰度(仅
  模拟盘) → 实盘; 禁止任何规则自动部署到实盘.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from . import db_ops
from .config import STOP_LOSS_PCT, TIME_STOP_MIN_GAIN, TIME_STOP_TRADING_DAYS

logger = logging.getLogger(__name__)

try:
    from simpleeval import simple_eval as _simple_eval
    _HAS_SIMPLEEVAL = True
except ImportError:
    _HAS_SIMPLEEVAL = False
    logger.info(
        "simpleeval 未安装 — 规则引擎降级为内置规则白名单; "
        "pip install 'simpleeval>=0.9.13' 启用表达式规则"
    )


# ---------------------------------------------------------------------------
# Hard-coded rules (预编译, 风控第一优先级)
# ---------------------------------------------------------------------------

HARD_RULES: dict[str, dict] = {
    "stop_loss_5pct": {
        "description": "亏损超过5%强制清仓 (连续两根K线或收盘确认)",
        "priority": 1,
        "fn": lambda ctx: float(ctx.get("current_loss_ratio", 0.0)) >= STOP_LOSS_PCT,
        "action": "sell_all",
    },
    "time_stop_5days": {
        "description": "买入后5个交易日无大行情(涨幅<3%且ATR低)清仓",
        "priority": 2,
        "fn": lambda ctx: (
            int(ctx.get("holding_trading_days", 0)) >= TIME_STOP_TRADING_DAYS
            and float(ctx.get("max_gain_ratio", 0.0)) < TIME_STOP_MIN_GAIN
            and float(ctx.get("atr_ratio", 1.0)) < 0.05
        ),
        "action": "sell_all",
    },
}


def eval_condition(condition: str, context: dict[str, Any]) -> bool:
    """求值一条规则条件表达式 (simpleeval → 白名单降级)."""
    if _HAS_SIMPLEEVAL:
        try:
            return bool(_simple_eval(condition, names=context))
        except Exception as exc:
            logger.warning("Rule eval failed (%s): %s", condition, exc)
            return False
    # 降级: 只支持已注册硬规则的条件 (精确字符串)
    for rule_id, rule in HARD_RULES.items():
        if condition.strip() == _seed_rules().get(rule_id, {}).get("condition"):
            try:
                return bool(rule["fn"](context))
            except Exception:
                return False
    logger.warning("simpleeval 缺失且条件未注册, fail-safe 返回 False: %s", condition)
    return False


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------

class RuleEngine:
    """规则引擎: 加载 DB 规则 + 内置硬规则, 按优先级求值."""

    def __init__(self):
        self._rules_cache: Optional[list[dict]] = None

    def reload(self) -> None:
        self._rules_cache = None

    def rules(self, include_disabled: bool = False) -> list[dict]:
        if self._rules_cache is None:
            self._rules_cache = db_ops.get_rules(enabled_only=not include_disabled)
        return self._rules_cache

    def evaluate(self, context: dict[str, Any]) -> list[dict]:
        """按优先级求值全部启用的规则, 返回触发的 [{rule_id, action, ...}]."""
        triggered = []
        # 1) 内置硬规则 (最高优先)
        for rule_id, rule in HARD_RULES.items():
            try:
                if rule["fn"](context):
                    triggered.append({
                        "rule_id": rule_id,
                        "action": rule["action"],
                        "description": rule["description"],
                        "source": "builtin",
                    })
            except Exception as exc:
                logger.warning("Hard rule %s failed: %s", rule_id, exc)
        # 2) DB 配置规则 (跳过与硬规则同名的)
        for rule in self.rules():
            if rule["rule_id"] in HARD_RULES:
                continue
            if eval_condition(rule["condition"], context):
                triggered.append({**rule, "source": "db"})
        triggered.sort(key=lambda r: r.get("priority", 5))
        return triggered


# ---------------------------------------------------------------------------
# 规则上线强制流水线 (§10.2) — 不可跳过
# ---------------------------------------------------------------------------

def run_rule_test_cases(rule_id: str) -> tuple[bool, list[dict]]:
    """执行规则的单元测试用例. 返回 (all_passed, results)."""
    engine_rule = next(
        (r for r in db_ops.get_rules() if r["rule_id"] == rule_id),
        None,
    )
    if engine_rule is None:
        return False, [{"error": f"rule {rule_id} not found"}]
    cases = db_ops.get_test_cases(rule_id)
    if not cases:
        return False, [{"error": "no test cases — 上线前必须配置用例"}]
    results = []
    all_passed = True
    for case in cases:
        try:
            actual = eval_condition(engine_rule["condition"], case["input"])
        except Exception as exc:
            actual = None
            case["error"] = str(exc)
        passed = actual == case["expected"]
        all_passed = all_passed and passed
        results.append({
            "case": case.get("case_name", case["id"]),
            "expected": case["expected"],
            "actual": actual,
            "passed": passed,
        })
    return all_passed, results


def validate_sample_out_perf(oos_metrics: dict, rule: dict) -> bool:
    """样本外绩效门槛校验 (夏普/最大回撤底线, §10.3)."""
    min_perf = float(rule.get("min_sample_out_perf", 0.0))
    sharpe = float(oos_metrics.get("sharpe", 0.0))
    max_dd = abs(float(oos_metrics.get("max_drawdown", 1.0)))
    if sharpe < max(min_perf, 0.5):
        return False
    if max_dd > 0.25:  # 硬回撤底线 25%
        return False
    return True


def submit_rule_candidate(rule: dict) -> dict:
    """新规则提交 → 强制流水线: 单测 → (样本外校验占位) → 人工审核队列.

    样本外回测需要历史数据, 由 evolution 模块异步补充; 这里至少强制
    单测通过且进入 pending_review, 未审核规则永远 enabled=False.
    """
    rule = {**rule, "enabled": False, "gray_scale": True}
    ok = db_ops.upsert_rule(rule)
    if not ok:
        return {"status": "invalid", "error": "rule_id 为空"}
    passed, results = run_rule_test_cases(rule["rule_id"])
    if not passed:
        db_ops.add_evolution_record({
            "rule_id": rule["rule_id"],
            "status": "test_failed",
            "reviewer_note": "单元测试未通过, 直接丢弃",
        })
        return {"status": "test_failed", "results": results}
    record_id = db_ops.add_evolution_record({
        "rule_id": rule["rule_id"],
        "params": {"condition": rule["condition"], "action": rule["action"]},
        "status": "pending_review",
    })
    return {"status": "pending_review", "evolution_id": record_id}


def approve_rule(rule_id: str, note: str = "") -> bool:
    """人工审核通过: 灰度开启 (仅模拟盘观察), 绝不直接实盘."""
    rule = next(
        (r for r in db_ops.get_rules() if r["rule_id"] == rule_id),
        None,
    )
    if rule is None:
        return False
    pending = [
        r for r in db_ops.get_evolution_records(status="pending_review", limit=20)
        if r["rule_id"] == rule_id
    ]
    if not pending:
        return False
    db_ops.update_evolution_status(pending[0]["id"], "approved", note)
    db_ops.upsert_rule({**rule, "enabled": True, "gray_scale": True})
    return True


def reject_rule(rule_id: str, note: str = "") -> bool:
    pending = [
        r for r in db_ops.get_evolution_records(status="pending_review", limit=20)
        if r["rule_id"] == rule_id
    ]
    if not pending:
        return False
    db_ops.update_evolution_status(pending[0]["id"], "rejected", note)
    return True


# ---------------------------------------------------------------------------
# Seed rules (首次初始化)
# ---------------------------------------------------------------------------

def _seed_rules() -> dict[str, dict]:
    return {
        "stop_loss_5pct": {
            "rule_id": "stop_loss_5pct",
            "description": "亏损超过5%强制清仓",
            "condition": "current_loss_ratio > 0.05",
            "action": "sell_all",
            "priority": 1,
            "enabled": True,
            "version": 1,
            "gray_scale": False,
        },
        "time_stop_5days": {
            "rule_id": "time_stop_5days",
            "description": "买入后5个交易日无大行情清仓",
            "condition": (
                "holding_trading_days >= 5 and max_gain_ratio < 0.03 and atr_ratio < 0.05"
            ),
            "action": "sell_all",
            "priority": 2,
            "enabled": True,
            "version": 1,
            "gray_scale": False,
        },
        "trailing_stop": {
            "rule_id": "trailing_stop",
            "description": "盈利超15%后止损位上移至成本+10% (收盘确认)",
            "condition": "current_gain_ratio > 0.15",
            "action": "raise_stop_loss",
            "priority": 3,
            "enabled": True,
            "version": 1,
            "gray_scale": False,
        },
        "single_position_cap": {
            "rule_id": "single_position_cap",
            "description": "单只持仓市值占比超过30%触发减仓建议",
            "condition": "position_value_pct > 0.30",
            "action": "suggest_reduce",
            "priority": 4,
            "enabled": True,
            "version": 1,
            "gray_scale": False,
        },
    }


def seed_rules_and_cases() -> None:
    """初始化默认规则 + 单测用例 (幂等)."""
    rules = _seed_rules()
    existing = {r["rule_id"] for r in db_ops.get_rules()}
    test_specs = {
        "stop_loss_5pct": [
            ({"current_loss_ratio": 0.06}, True),
            ({"current_loss_ratio": 0.04}, False),
        ],
        "time_stop_5days": [
            ({"holding_trading_days": 6, "max_gain_ratio": 0.01, "atr_ratio": 0.02}, True),
            ({"holding_trading_days": 3, "max_gain_ratio": 0.01, "atr_ratio": 0.02}, False),
            ({"holding_trading_days": 6, "max_gain_ratio": 0.05, "atr_ratio": 0.02}, False),
        ],
        "trailing_stop": [
            ({"current_gain_ratio": 0.16}, True),
            ({"current_gain_ratio": 0.10}, False),
        ],
        "single_position_cap": [
            ({"position_value_pct": 0.35}, True),
            ({"position_value_pct": 0.20}, False),
        ],
    }
    for rule_id, rule in rules.items():
        if rule_id in existing:
            continue
        db_ops.upsert_rule(rule)
        for i, (ctx, expected) in enumerate(test_specs.get(rule_id, []), 1):
            db_ops.add_test_case(
                rule_id, f"case_{i}", ctx, expected,
            )
    logger.info("Seed rules ensured (%d rules)", len(rules))


_engine: Optional[RuleEngine] = None


def get_rule_engine() -> RuleEngine:
    global _engine
    if _engine is None:
        _engine = RuleEngine()
    return _engine
