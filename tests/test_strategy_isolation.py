"""隔离性测试.

把"分析结论不许碰交易"从口头纪律变成 CI 门禁:
策略台包内一旦 import broker / risk_control / 交易 Agent, 这个用例就红.

同样的思路也守着"纯函数层不许碰 IO" —— rules/ 一旦 import 数据库或网络,
20 个交易日的回放能力立刻消失, 而这种腐化是静默的.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STRATEGY_DIR = Path(__file__).resolve().parents[1] / "ai_stock" / "quant" / "strategy"

FORBIDDEN_IN_STRATEGY = ("broker", "risk_control", "agents.buy", "agents.hold", "orchestrator")
FORBIDDEN_IN_RULES = ("sqlalchemy", "requests", "aiohttp", "llm_helper", "db_ops", "data_service")


def _py_files(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*.py") if "__pycache__" not in p.parts]


def test_strategy_never_imports_trading_chain():
    hits = []
    for path in _py_files(STRATEGY_DIR):
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_IN_STRATEGY:
            if re.search(rf"^\s*(from|import)\s+.*{re.escape(token)}", text, re.M):
                hits.append(f"{path.name}: {token}")
    assert not hits, f"策略台不得依赖交易链路: {hits}"


def test_rules_stay_pure():
    """rules/ 只能依赖 contracts 与标准库 —— 这是回放能力的物理边界."""
    hits = []
    for path in _py_files(STRATEGY_DIR / "rules"):
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_IN_RULES:
            if re.search(rf"^\s*(from|import)\s+.*{re.escape(token)}", text, re.M):
                hits.append(f"{path.name}: {token}")
    assert not hits, f"rules 层出现 IO 依赖: {hits}"


def test_intraday_claim_rejected_by_repo():
    """盘中断言不得入账 —— 入库即污染胜率统计."""
    from datetime import date

    from ai_stock.quant.strategy.contracts import Claim
    from ai_stock.quant.strategy.repo import StrategyRepo

    claim = Claim(
        claim_id="P1-M1", period="2026-10-24-P1", type="MAINLINE",
        target="X·Y", direction="上行", verify_date=date(2026, 10, 27),
        snapshot_type="intraday",
    )
    with pytest.raises(ValueError, match="intraday"):
        StrategyRepo()._insert_claims(None, [claim])


def test_effective_from_only_for_close(monkeypatch):
    """只有收盘报告才有生效时点; 盘中快照永远是空串 (交易链路读不到)."""
    from ai_stock.quant.strategy import service

    monkeypatch.setattr(
        "ai_stock.quant.calendar_utils.next_trading_day",
        lambda ref=None: ref,
    )
    assert service._effective_from("2026-10-24", "close") == "2026-10-24 09:15"
    assert service._effective_from("2026-10-24", "intraday") == ""
