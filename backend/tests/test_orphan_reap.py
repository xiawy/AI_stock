"""B9 regression: startup must freeze orphaned running/paused task rows.

After a service restart no in-memory tracker exists for rows still marked
running/paused; ``sync_task_row`` would leave them frozen forever. The
startup reaper must mark them stopped (with a human-readable note) while
leaving terminal rows (completed/error/stopped) untouched.
"""

from __future__ import annotations

import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.core.database as database_module
import app.models  # noqa: F401 — register all tables on Base.metadata
from app.core.database import Base
from app.models.analysis_task import AnalysisTask
from app.models.user import User


def _make_task(user_id: int, status: str) -> AnalysisTask:
    return AnalysisTask(
        id=uuid.uuid4().hex,
        user_id=user_id,
        ticker="600519",
        trade_date="2026-06-01",
        status=status,
    )


def test_reap_orphaned_tasks_freezes_running_and_paused(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'reap.db'}")
    Base.metadata.create_all(bind=engine)
    reap_session = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr(database_module, "SessionLocal", reap_session)

    with reap_session() as session:
        user = User(username="reap-user", email="reap@example.com", password_hash="x")
        session.add(user)
        session.commit()
        session.add_all(
            [
                _make_task(user.id, "running"),
                _make_task(user.id, "paused"),
                _make_task(user.id, "completed"),
                _make_task(user.id, "error"),
            ]
        )
        session.commit()

    from app.main import _reap_orphaned_tasks

    _reap_orphaned_tasks()

    with reap_session() as session:
        rows = session.query(AnalysisTask).all()

    assert sorted(row.status for row in rows) == [
        "completed",
        "error",
        "stopped",
        "stopped",
    ]
    for row in rows:
        if row.status == "stopped":
            assert "服务重启" in row.error
