"""多 worker 下的量化子系统领导者选举（A5 配套）。

`QuantService.start()` 会起消费者 + APScheduler；`uvicorn --workers N` 时每个
worker 都跑 lifespan，若不加约束就会出现 N 份调度器同时发任务。这里用文件锁
保证**同一时刻只有一个 worker** 拥有交易子系统，并验证：

1. 同进程重复 acquire 幂等；
2. 另一个进程在锁被持有时拿不到；
3. 释放后（含进程退出）新进程可以接手。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_CHILD = (
    "import sys\n"
    "from ai_stock.quant import leader\n"
    "sys.exit(0 if leader.acquire(sys.argv[1]) else 3)\n"
)


def _child_acquire(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _CHILD, str(path)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.unit
class TestQuantLeaderLock:
    def test_acquire_is_idempotent(self, tmp_path):
        from ai_stock.quant import leader

        path = tmp_path / "leader.lock"
        try:
            assert leader.acquire(path) is True
            assert leader.is_leader() is True
            assert leader.acquire(path) is True       # 同进程重复调用不重复加锁
            assert path.exists()
        finally:
            leader.release()
            assert leader.is_leader() is False

    def test_other_process_is_blocked(self, tmp_path):
        from ai_stock.quant import leader

        path = tmp_path / "leader.lock"
        try:
            assert leader.acquire(path) is True
            proc = _child_acquire(path)
            assert proc.returncode == 3, proc.stderr
        finally:
            leader.release()

    def test_next_process_takes_over_after_release(self, tmp_path):
        from ai_stock.quant import leader

        path = tmp_path / "leader.lock"
        assert leader.acquire(path) is True
        leader.release()

        proc = _child_acquire(path)
        assert proc.returncode == 0, proc.stderr
