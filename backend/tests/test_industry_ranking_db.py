"""IndustryRanking persistence tests (行业榜).

- save_industry_rankings / get_latest_industry_rankings / by-date roundtrip
- rows cascade-delete with their ImpactSnapshot (70-day cleanup covers 行业榜)
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.core.database as core_db
from ai_stock.pipeline import db_ops
from ai_stock.pipeline.db_models import ImpactSnapshot, IndustryRanking
from app.services import cleanup


@pytest.fixture()
def ops_db(tmp_path, monkeypatch):
    """Isolated engine bound to whatever Base db_models actually uses.

    In a full-suite run some earlier test makes ``backend.app.*`` importable
    (namespace package via the editable install), so ``db_ops._get_session``
    may resolve a *different* module instance than the one conftest patches.
    Patching ``_get_session`` directly sidesteps the import-order guessing.
    """
    from ai_stock.pipeline import db_models

    engine = create_engine(
        f"sqlite:///{tmp_path / 'industry.db'}",
        connect_args={"check_same_thread": False},
    )
    db_models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    monkeypatch.setattr(db_ops, "_get_session", lambda: session)
    # cleanup_ranking_snapshots does ``from app.core.database import SessionLocal``
    monkeypatch.setattr(core_db, "SessionLocal", lambda: session)
    # Defensive: patch the other module instance too when it got imported
    if "backend.app.core.database" in sys.modules:
        import backend.app.core.database as root_core_db

        monkeypatch.setattr(root_core_db, "SessionLocal", lambda: session)

    yield session
    session.close()
    engine.dispose()


def _mk_snapshot(db, status="completed", days_ago=0):
    snap = ImpactSnapshot(
        period="AM",
        status=status,
        snapshot_time=datetime.now(timezone.utc) - timedelta(days=days_ago),
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db.add(snap)
    db.commit()
    return snap


def test_save_and_latest_roundtrip(ops_db):
    db = ops_db
    snap = _mk_snapshot(db)
    snap_id = snap.id  # capture before db_ops closes the session (detaches rows)
    rankings = [
        {
            "industry": "电子",
            "industry_code": "BK1033",
            "heat_score": 9.5,
            "news_count": 4,
            "fund_flow_net": 5.2e9,
            "change_pct": 2.5,
            "resonance": "strong",
            "rating": "A",
            "leader_stocks": [
                {"code": "000063", "name": "中兴通讯", "change_pct": 5.1, "market_cap": 1.5e11},
            ],
            "rank": 1,
        },
        {
            "industry": "银行",
            "heat_score": 3.2,
            "news_count": 2,
            "fund_flow_net": None,
            "change_pct": None,
            "resonance": "none",
            "rating": "C",
            "leader_stocks": [],
            "rank": 2,
        },
    ]

    assert db_ops.save_industry_rankings(snap_id, rankings) == 2

    result = db_ops.get_latest_industry_rankings()
    assert result is not None
    assert result["snapshot"]["id"] == snap_id
    assert [r["rank"] for r in result["rankings"]] == [1, 2]

    top = result["rankings"][0]
    assert top["industry"] == "电子"
    assert top["leader_stocks"][0]["code"] == "000063"
    second = result["rankings"][1]
    assert second["fund_flow_net"] is None
    assert second["leader_stocks"] == []


def test_latest_prefers_most_recent_completed(ops_db):
    db = ops_db
    old = _mk_snapshot(db, days_ago=2)
    old_id = old.id
    db_ops.save_industry_rankings(old_id, [{"industry": "旧行业", "rank": 1, "heat_score": 1.0}])
    new = _mk_snapshot(db, days_ago=0)
    new_id = new.id
    db_ops.save_industry_rankings(new_id, [{"industry": "新行业", "rank": 1, "heat_score": 2.0}])

    result = db_ops.get_latest_industry_rankings()

    assert result["snapshot"]["id"] == new_id
    assert result["rankings"][0]["industry"] == "新行业"


def test_history_by_date(ops_db):
    db = ops_db
    snap = _mk_snapshot(db, days_ago=1)
    db_ops.save_industry_rankings(snap.id, [{"industry": "电子", "rank": 1, "heat_score": 8.0}])

    day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    result = db_ops.get_industry_rankings_by_date(day)
    assert result is not None
    assert result["rankings"][0]["industry"] == "电子"

    # No data for an unrelated date → None (API layer renders empty)
    assert db_ops.get_industry_rankings_by_date("2020-01-01") is None


def test_industry_rows_cascade_with_snapshot(ops_db):
    """行业榜 rows live and die with their snapshot — the 70-day ranking
    cleanup covers industry_rankings via ORM cascade."""
    db = ops_db
    snap = _mk_snapshot(db, days_ago=80)
    db_ops.save_industry_rankings(
        snap.id, [{"industry": "电子", "rank": 1, "heat_score": 8.0}],
    )
    db.expire_all()

    removed = cleanup.cleanup_ranking_snapshots(max_age_days=70)

    assert removed == 1
    assert db.query(IndustryRanking).count() == 0
    assert db.query(ImpactSnapshot).count() == 0


def test_cleanup_keeps_recent_industry_rows(ops_db):
    db = ops_db
    snap = _mk_snapshot(db, days_ago=5)
    db_ops.save_industry_rankings(
        snap.id, [{"industry": "电子", "rank": 1, "heat_score": 8.0}],
    )

    removed = cleanup.cleanup_ranking_snapshots(max_age_days=70)

    assert removed == 0
    assert db.query(IndustryRanking).count() == 1
