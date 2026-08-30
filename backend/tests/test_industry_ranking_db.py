
"""QuantIndustryBoard persistence tests (行业榜 = quant 选股流程产出).

- save_industry_board / get_latest_industry_board / by-date roundtrip
- 覆盖写入语义 (同日重写)
- cleanup_industry_board 保留窗口 (70 天, 与新闻榜一致)
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import ai_stock.quant.db as quant_db_mod
from ai_stock.quant import db_ops


@pytest.fixture()
def quant_db(tmp_path, monkeypatch):
    """Point the quant engine at an isolated SQLite file and create tables."""
    monkeypatch.setenv(
        "QUANT_DB_URL", f"sqlite:///{tmp_path / 'quant_test.db'}",
    )
    quant_db_mod._engine = None
    quant_db_mod._SessionFactory = None
    try:
        quant_db_mod.init_quant_db()
        yield
    finally:
        if quant_db_mod._engine is not None:
            quant_db_mod._engine.dispose()
        quant_db_mod._engine = None
        quant_db_mod._SessionFactory = None


def _rows(*industries, base_date: str = "2026-08-20") -> list[dict]:
    """Build minimal board rows ranked 1..N."""
    return [
        {
            "rank": i + 1,
            "industry": name,
            "industry_code": f"BK{1000 + i}",
            "industry_level": "industry",
            "stage": "成长期",
            "event_tag": "政策",
            "heat_score": 9.0 - i,
            "change_pct": 2.5 - i * 0.1,
            "main_net_inflow": 5.2e9 if i == 0 else None,
            "leader_stocks": (
                [{"code": "000063", "name": "中兴通讯", "leader_label": "精选"}]
                if i == 0 else []
            ),
        }
        for i, name in enumerate(industries)
    ]


def test_save_and_latest_roundtrip(quant_db):
    assert db_ops.save_industry_board("2026-08-20", _rows("电子", "银行")) == 2

    result = db_ops.get_latest_industry_board()
    assert result is not None
    assert result["rank_date"] == "2026-08-20"
    assert [r["rank"] for r in result["rankings"]] == [1, 2]

    top = result["rankings"][0]
    assert top["industry"] == "电子"
    assert top["industry_code"] == "BK1000"
    assert top["stage"] == "成长期"
    assert top["leader_stocks"][0]["code"] == "000063"
    second = result["rankings"][1]
    assert second["main_net_inflow"] is None
    assert second["leader_stocks"] == []


def test_latest_prefers_most_recent_date(quant_db):
    db_ops.save_industry_board("2026-08-18", _rows("旧行业"))
    db_ops.save_industry_board("2026-08-20", _rows("新行业"))

    result = db_ops.get_latest_industry_board()
    assert result["rank_date"] == "2026-08-20"
    assert result["rankings"][0]["industry"] == "新行业"


def test_history_by_date(quant_db):
    db_ops.save_industry_board("2026-08-19", _rows("电子"))

    result = db_ops.get_industry_board_by_date("2026-08-19")
    assert result is not None
    assert result["rankings"][0]["industry"] == "电子"

    # No data for an unrelated date → None (API layer renders empty)
    assert db_ops.get_industry_board_by_date("2020-01-01") is None


def test_same_day_rewrite_overwrites(quant_db):
    """选股流程每日可多次运行 — 同日行业榜覆盖写入, 不产生重复."""
    db_ops.save_industry_board("2026-08-20", _rows("旧榜"))
    db_ops.save_industry_board("2026-08-20", _rows("新榜", "第二行业"))

    result = db_ops.get_industry_board_by_date("2026-08-20")
    assert [r["industry"] for r in result["rankings"]] == ["新榜", "第二行业"]


def test_board_row_by_id(quant_db):
    db_ops.save_industry_board("2026-08-20", _rows("电子"))
    row = db_ops.get_industry_board_by_date("2026-08-20")["rankings"][0]

    fetched = db_ops.get_industry_board_row(row["id"])
    assert fetched is not None
    assert fetched["industry"] == "电子"
    assert db_ops.get_industry_board_row(999999) is None


def test_cleanup_respects_retention_window(quant_db):
    old_day = (date.today() - timedelta(days=80)).isoformat()
    recent_day = (date.today() - timedelta(days=5)).isoformat()
    db_ops.save_industry_board(old_day, _rows("过期行业"))
    db_ops.save_industry_board(recent_day, _rows("近期行业"))

    removed = db_ops.cleanup_industry_board(days=70)

    assert removed == 1
    assert db_ops.get_industry_board_by_date(old_day) is None
    assert db_ops.get_industry_board_by_date(recent_day) is not None
