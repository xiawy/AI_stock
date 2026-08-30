"""Tests for the ranking backup schedule (23:30 daily).

Covers:
- BACKUP_DAILY_AT constant
- Scheduler job registration (daily backup job only; the heavy impact
  pipeline was removed — the news board is written by the selection flow)
- Startup compensation for a missed backup slot (ensure_today_backup)
- backup_today_data idempotency / empty-data skip / payload shape
- cleanup_old_backups retention window
"""

from __future__ import annotations

import json

import pytest

from ai_stock.pipeline import backup as backup_mod
from ai_stock.pipeline import scheduler as scheduler_mod
from ai_stock.pipeline.config import BACKUP_DAILY_AT
from ai_stock.pipeline.scheduler import PipelineScheduler


# ---------------------------------------------------------------------------
# Schedule constants
# ---------------------------------------------------------------------------


def test_backup_daily_at_constant():
    assert BACKUP_DAILY_AT == (23, 30)


# ---------------------------------------------------------------------------
# Scheduler job registration (needs APScheduler installed)
# ---------------------------------------------------------------------------


def test_scheduler_registers_backup_job():
    pytest.importorskip("apscheduler")

    sched = PipelineScheduler({})
    sched.start()
    try:
        jobs = {job.id: job for job in sched._scheduler.get_jobs()}
        assert list(jobs) == ["pipeline_backup"]
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


def _stub_db(monkeypatch, core=None, industry=None, hot_stocks=None):
    from ai_stock.pipeline import db_ops
    from ai_stock.quant import db_ops as quant_db_ops

    monkeypatch.setattr(db_ops, "get_snapshot_by_date", lambda d: core)
    monkeypatch.setattr(
        quant_db_ops, "get_industry_board_by_date", lambda d: industry,
    )
    monkeypatch.setattr(
        quant_db_ops, "get_optional_pool_as_of", lambda d: hot_stocks or [],
    )


def test_backup_writes_three_boards(monkeypatch, isolated_backup):
    core = {
        "snapshot": {"id": 7},
        "news_items": [{"title": "news-1"}],
    }
    industry = {"rank_date": "2026-08-17", "rankings": [{"industry": "白酒"}]}
    hot_stocks = [{"symbol": "600519", "name": "贵州茅台"}]
    _stub_db(monkeypatch, core=core, industry=industry, hot_stocks=hot_stocks)

    result = backup_mod.backup_today_data("2026-08-17")
    assert result["status"] == "completed"

    payload = json.loads(
        (isolated_backup / "rankings_2026-08-17.json").read_text("utf-8"),
    )
    assert payload["date"] == "2026-08-17"
    assert payload["news_items"] == [{"title": "news-1"}]  # 新闻榜
    assert payload["industry_rankings"] == [{"industry": "白酒"}]  # 行业榜 (quant)
    assert payload["recommendations"] == hot_stocks  # 热股榜 = 自选池快照


def test_backup_is_idempotent(monkeypatch, isolated_backup):
    _stub_db(monkeypatch, core={"snapshot": {}, "news_items": []})
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
