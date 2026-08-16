"""Tests for app.services.cleanup — data-retention policy.

诊股 records (analysis_tasks rows + on-disk reports + resumable-task index)
are removed after 20 days; ranking snapshots (news items + recommendations)
are removed after 70 days, cascading to their children.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

import app.core.database as core_db
import web.history as web_history
from ai_stock.pipeline.db_models import ImpactSnapshot, NewsItem, StockRecommendation
from app.models.analysis_task import AnalysisTask
from app.models.user import User
from app.services import cleanup


class _SessionCtx:
    """Context-manager shim so cleanup()'s ``with SessionLocal()`` runs on the
    test session (the real SessionLocal points at a different, empty DB)."""

    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def cleanup_db(db_session, monkeypatch):
    monkeypatch.setattr(core_db, "SessionLocal", lambda: _SessionCtx(db_session))
    return db_session


@pytest.fixture()
def results_dir(tmp_path, monkeypatch):
    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.setattr(web_history, "_RESULTS_DIR", results)
    monkeypatch.setattr(
        web_history, "_INCOMPLETE_TASKS_FILE", tmp_path / "incomplete_tasks.json"
    )
    return results


def _days_ago(n: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=n)


def _add_user(db) -> User:
    user = User(username="u1", email="u1@example.com", password_hash="x")
    db.add(user)
    db.flush()
    return user


def test_diagnosis_tasks_retention(cleanup_db, results_dir):
    db = cleanup_db
    user = _add_user(db)
    db.add(
        AnalysisTask(
            id="old",
            user_id=user.id,
            ticker="600519",
            trade_date="2026-01-01",
            status="completed",
            created_at=_days_ago(30),
        )
    )
    db.add(
        AnalysisTask(
            id="recent",
            user_id=user.id,
            ticker="000001",
            trade_date="2026-08-01",
            status="completed",
            created_at=_days_ago(5),
        )
    )
    db.commit()

    stats = cleanup.cleanup_diagnosis_data(max_age_days=20)

    assert {t.id for t in db.query(AnalysisTask).all()} == {"recent"}
    assert stats["tasks"] == 1


def test_diagnosis_files_retention(cleanup_db, results_dir):
    old_date = (date.today() - timedelta(days=25)).isoformat()
    new_date = (date.today() - timedelta(days=2)).isoformat()

    old_dir = results_dir / "600519" / old_date
    old_dir.mkdir(parents=True)
    old_file = old_dir / f"full_states_log_{old_date}.json"
    old_file.write_text("{}", encoding="utf-8")

    new_dir = results_dir / "000001" / new_date
    new_dir.mkdir(parents=True)
    new_file = new_dir / f"full_states_log_{new_date}.json"
    new_file.write_text("{}", encoding="utf-8")

    web_history._INCOMPLETE_TASKS_FILE.write_text(
        json.dumps(
            [
                {"task_id": "t-old", "ticker": "600519", "trade_date": old_date},
                {"task_id": "t-new", "ticker": "000001", "trade_date": new_date},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    stats = cleanup.cleanup_diagnosis_data(max_age_days=20)

    assert not old_file.exists()
    # Emptied <ticker>/<date> directories are pruned along the way
    assert not old_dir.exists()
    assert not (results_dir / "600519").exists()
    assert new_file.exists()

    kept = json.loads(
        web_history._INCOMPLETE_TASKS_FILE.read_text(encoding="utf-8")
    )
    assert [e["task_id"] for e in kept] == ["t-new"]
    assert stats["files"] == 1
    assert stats["incomplete"] == 1


def test_ranking_snapshots_retention(cleanup_db):
    db = cleanup_db

    old_snap = ImpactSnapshot(
        period="AM",
        status="completed",
        snapshot_time=_days_ago(80),
        created_at=_days_ago(80),
    )
    old_snap.news_items.append(NewsItem(title_hash="h1", title="old news"))
    old_snap.recommendations.append(StockRecommendation(ticker="600519"))

    new_snap = ImpactSnapshot(
        period="PM",
        status="completed",
        snapshot_time=_days_ago(10),
        created_at=_days_ago(10),
    )
    new_snap.news_items.append(NewsItem(title_hash="h2", title="new news"))
    new_snap.recommendations.append(StockRecommendation(ticker="000001"))

    db.add_all([old_snap, new_snap])
    db.commit()
    db.expire_all()
    remaining_id = new_snap.id

    removed = cleanup.cleanup_ranking_snapshots(max_age_days=70)

    assert removed == 1
    assert {s.id for s in db.query(ImpactSnapshot).all()} == {remaining_id}
    # ORM cascade removed the old snapshot's children only
    assert {n.title for n in db.query(NewsItem).all()} == {"new news"}
    assert {r.ticker for r in db.query(StockRecommendation).all()} == {"000001"}


def test_run_all_cleanup_is_fault_tolerant(cleanup_db, results_dir, monkeypatch):
    db = cleanup_db
    user = _add_user(db)
    db.add(
        AnalysisTask(
            id="old",
            user_id=user.id,
            ticker="600519",
            trade_date="2026-01-01",
            status="completed",
            created_at=_days_ago(30),
        )
    )
    db.commit()

    def _boom():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(cleanup, "cleanup_ranking_snapshots", _boom)

    stats = cleanup.run_all_cleanup()

    assert stats["diagnosis"]["tasks"] == 1
    assert stats["rankings"] == "error"
    assert {t.id for t in db.query(AnalysisTask).all()} == set()
