"""Tests for the ranking schedule (00:00/08:30/12:30/14:30 + 23:30 backup).

Covers:
- PIPELINE_SCHEDULE / BACKUP_DAILY_AT constants and slot parsing
- Scheduler job registration (pipeline slots + daily backup job)
- Startup compensation for a missed backup slot (ensure_today_backup)
- backup_today_data idempotency / empty-data skip / payload shape
- cleanup_old_backups retention window
"""

from __future__ import annotations

import json

import pytest

from ai_stock.pipeline import backup as backup_mod
from ai_stock.pipeline import scheduler as scheduler_mod
from ai_stock.pipeline.config import BACKUP_DAILY_AT, PIPELINE_SCHEDULE
from ai_stock.pipeline.scheduler import PipelineScheduler, parse_schedule


# ---------------------------------------------------------------------------
# Schedule constants
# ---------------------------------------------------------------------------


def test_pipeline_schedule_matches_required_slots():
    assert PIPELINE_SCHEDULE == ["00:00", "08:30", "12:30", "14:30"]
    assert BACKUP_DAILY_AT == (23, 30)


def test_parse_schedule_handles_new_slots():
    assert parse_schedule(PIPELINE_SCHEDULE) == [
        (0, 0), (8, 30), (12, 30), (14, 30),
    ]


def test_scheduler_slots_default():
    sched = PipelineScheduler({})
    assert sched.schedule_slots == [(0, 0), (8, 30), (12, 30), (14, 30)]


# ---------------------------------------------------------------------------
# Scheduler job registration (needs APScheduler installed)
# ---------------------------------------------------------------------------


def test_scheduler_registers_backup_job():
    pytest.importorskip("apscheduler")

    sched = PipelineScheduler({})
    sched.start()
    try:
        jobs = {job.id: job for job in sched._scheduler.get_jobs()}
        for slot in ((0, 0), (8, 30), (12, 30), (14, 30)):
            assert f"pipeline_{slot[0]:02d}{slot[1]:02d}" in jobs
        assert "pipeline_backup" in jobs
    finally:
        sched.stop()


# ---------------------------------------------------------------------------
# ensure_today_backup startup compensation
# ---------------------------------------------------------------------------


class _FakeThread:
    """Runs the target synchronously so tests can assert immediately."""

    def __init__(self, target, name=None, daemon=None):
        self._target = target
        self.name = name

    def start(self):
        self._target()


@pytest.fixture()
def sync_threads(monkeypatch):
    monkeypatch.setattr(scheduler_mod.threading, "Thread", _FakeThread)


def test_ensure_today_backup_runs_after_slot(monkeypatch, sync_threads):
    sched = PipelineScheduler({})

    # Pretend the backup slot has already passed and no backup exists.
    monkeypatch.setattr(scheduler_mod, "BACKUP_DAILY_AT", (0, 0))
    monkeypatch.setattr(backup_mod, "backup_exists_for_date", lambda d: False)

    calls = []
    monkeypatch.setattr(sched, "_run_backup", lambda: calls.append(1) or {})
    sched.ensure_today_backup()
    assert calls == [1]


def test_ensure_today_backup_skips_when_exists(monkeypatch, sync_threads):
    sched = PipelineScheduler({})

    monkeypatch.setattr(scheduler_mod, "BACKUP_DAILY_AT", (0, 0))
    monkeypatch.setattr(backup_mod, "backup_exists_for_date", lambda d: True)

    calls = []
    monkeypatch.setattr(sched, "_run_backup", lambda: calls.append(1) or {})
    sched.ensure_today_backup()
    assert calls == []


def test_ensure_today_backup_noop_before_slot(monkeypatch, sync_threads):
    sched = PipelineScheduler({})

    # Backup slot far in the future → nothing happens regardless of state.
    monkeypatch.setattr(
        scheduler_mod, "BACKUP_DAILY_AT", (23, 59),
    )
    monkeypatch.setattr(backup_mod, "backup_exists_for_date", lambda d: False)

    calls = []
    monkeypatch.setattr(sched, "_run_backup", lambda: calls.append(1) or {})
    sched.ensure_today_backup()
    assert calls == []


# ---------------------------------------------------------------------------
# backup_today_data / cleanup_old_backups (DB reads are stubbed)
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_backup(monkeypatch, tmp_path):
    """Point the backup dir at a tmp dir and stub the DB reads."""
    monkeypatch.setattr(backup_mod, "_backup_dir", lambda: tmp_path)
    return tmp_path


def _stub_db(monkeypatch, core=None, industry=None):
    from ai_stock.pipeline import db_ops

    monkeypatch.setattr(db_ops, "get_snapshot_by_date", lambda d: core)
    monkeypatch.setattr(
        db_ops, "get_industry_rankings_by_date", lambda d: industry,
    )


def test_backup_writes_three_boards(monkeypatch, isolated_backup):
    core = {
        "snapshot": {"id": 7},
        "news_items": [{"title": "news-1"}],
        "recommendations": [{"ticker": "600519"}],
    }
    industry = {"snapshot": {"id": 7}, "rankings": [{"industry": "白酒"}]}
    _stub_db(monkeypatch, core=core, industry=industry)

    result = backup_mod.backup_today_data("2026-08-17")
    assert result["status"] == "completed"

    payload = json.loads(
        (isolated_backup / "rankings_2026-08-17.json").read_text("utf-8"),
    )
    assert payload["date"] == "2026-08-17"
    assert payload["news_items"] == [{"title": "news-1"}]  # 新闻榜
    assert payload["industry_rankings"] == [{"industry": "白酒"}]  # 行业榜
    assert payload["recommendations"] == [{"ticker": "600519"}]  # 热股榜


def test_backup_is_idempotent(monkeypatch, isolated_backup):
    _stub_db(monkeypatch, core={"snapshot": {}, "news_items": [], "recommendations": []})
    assert backup_mod.backup_today_data("2026-08-17")["status"] == "completed"
    assert backup_mod.backup_today_data("2026-08-17")["status"] == "skipped"


def test_backup_skips_when_no_data(monkeypatch, isolated_backup):
    _stub_db(monkeypatch, core=None, industry=None)
    result = backup_mod.backup_today_data("2026-08-17")
    assert result["status"] == "skipped"
    assert not (isolated_backup / "rankings_2026-08-17.json").exists()


def test_cleanup_old_backups_respects_window(isolated_backup):
    from datetime import date, timedelta

    today = date.today()
    (isolated_backup / f"rankings_{today.isoformat()}.json").write_text("{}", "utf-8")
    old = (today - timedelta(days=71)).isoformat()
    (isolated_backup / f"rankings_{old}.json").write_text("{}", "utf-8")
    (isolated_backup / "unrelated.txt").write_text("x", "utf-8")

    removed = backup_mod.cleanup_old_backups(keep_days=70)
    assert removed == 1
    assert (isolated_backup / f"rankings_{today.isoformat()}.json").exists()
    assert not (isolated_backup / f"rankings_{old}.json").exists()
    assert (isolated_backup / "unrelated.txt").exists()
