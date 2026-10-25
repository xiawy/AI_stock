"""``create_all`` 多 worker 竞态兜底的回归用例。

背景（2026-10-25 真实事故）：上线新增 ``news_cluster_member`` 表后重启生产，
uvicorn ``--workers 2`` 的两个 worker 并行 startup，``checkfirst`` 双方都看不到
表，于是都发 ``CREATE TABLE``。后到的那个抛 ``table ... already exists``，worker
退出，父进程随即停摆；因为父进程退出码是 0，systemd 的 ``Restart=on-failure``
不会拉起，服务静默下线。

这些用例锁住两件事：``already exists`` 必须重试，其它 ``OperationalError``
必须原样抛出（真正的 schema 问题不能被兜底逻辑吞掉）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.schema import MetaData

from ai_stock.quant import db as quant_db
from ai_stock.quant.db import create_all_race_safe


def _op_error(message: str) -> OperationalError:
    """构造一个带原始信息量的 OperationalError。"""
    return OperationalError("CREATE TABLE demo", {}, Exception(message))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """把退避睡眠打掉，避免用例真的等 0.6s。"""
    monkeypatch.setattr(quant_db.time, "sleep", lambda *_: None)


def test_retries_when_table_already_created_by_other_worker(monkeypatch):
    """另一个 worker 抢先建表 → 重试一次即通过，不向上抛。"""
    calls = {"n": 0}

    def fake_create_all(self, bind=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _op_error("table news_cluster_member already exists")

    monkeypatch.setattr(MetaData, "create_all", fake_create_all)

    create_all_race_safe(MetaData(), bind=object())

    assert calls["n"] == 2, "应当重试且只重试一次"


def test_other_operational_errors_are_not_swallowed(monkeypatch):
    """I/O 错误等真问题必须原样抛出，不能被当成竞态吃掉。"""

    def fake_create_all(self, bind=None, **kwargs):
        raise _op_error("disk I/O error")

    monkeypatch.setattr(MetaData, "create_all", fake_create_all)

    with pytest.raises(OperationalError) as excinfo:
        create_all_race_safe(MetaData(), bind=object())

    assert "disk I/O error" in str(excinfo.value)


def test_reraises_after_attempts_exhausted(monkeypatch):
    """一直 already exists（例如建表语句本身有问题）要放弃并抛出。"""
    calls = {"n": 0}

    def fake_create_all(self, bind=None, **kwargs):
        calls["n"] += 1
        raise _op_error("table demo already exists")

    monkeypatch.setattr(MetaData, "create_all", fake_create_all)

    with pytest.raises(OperationalError):
        create_all_race_safe(MetaData(), bind=object(), attempts=3)

    assert calls["n"] == 3


def test_success_path_does_not_retry(monkeypatch):
    calls = {"n": 0}

    def fake_create_all(self, bind=None, **kwargs):
        calls["n"] += 1

    monkeypatch.setattr(MetaData, "create_all", fake_create_all)

    create_all_race_safe(MetaData(), bind=object())

    assert calls["n"] == 1


def test_idempotent_on_real_sqlite(tmp_path):
    """真实库上重复调用不报错（等价于第二个 worker 的正常路径）。"""

    class _Base(DeclarativeBase):
        pass

    engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}")
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS demo (id INTEGER)"))
        create_all_race_safe(_Base.metadata, engine)
        create_all_race_safe(_Base.metadata, engine)
    finally:
        engine.dispose()
