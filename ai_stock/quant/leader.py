"""单机多 worker 下的「量化子系统领导者」选举（文件锁）。

背景：``QuantService.start()`` 会在**当前进程**里起 MQ 消费者、延迟队列 worker、
超时清扫器和 APScheduler。uvicorn ``--workers N`` 时每个 worker 都会跑 lifespan，
若不加约束就会有 N 份调度器同时发任务 —— 即便幂等键能挡住重复下单，也会带来
N 倍的扫描与 LLM 开销，且超时清扫会互相踩。

A5 让分析任务的状态可以跨 worker 共享，但**交易子系统必须单实例**。这里用一把
进程级文件锁做领导者选举：

- 抢到锁的 worker 启动量化子系统；其余 worker 只提供 API（含交易查询）；
- 领导者进程退出（含崩溃）时内核自动释放锁，uvicorn 拉起的新 worker 会接手；
- 锁文件放在数据目录，不随 git 走，也不影响单 worker 部署（行为完全等价）。

跨主机的多实例不在支持范围：那种部署需要 Redis/DB 级的分布式锁，而本项目的
交易链路本来就绑定单机 SQLite。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK_FILENAME = "quant_leader.lock"

# 进程存活期间必须保持打开（关闭即释放锁）
_handle = None
_lock_path: Path | None = None


def _default_lock_path() -> Path:
    """数据目录下的锁文件（与 quant SQLite 同目录，便于运维一眼看到）。"""
    try:
        from .db import _resolve_db_url

        url = _resolve_db_url()
        if url.startswith("sqlite:///"):
            data_dir = Path(url.removeprefix("sqlite:///")).parent
        else:  # pragma: no cover - 非 SQLite 部署退化到工作目录
            data_dir = Path(os.environ.get("PROJECT_ROOT", ".")) / "data"
    except Exception:  # pragma: no cover - 引擎缺失时退化到工作目录
        data_dir = Path(os.environ.get("PROJECT_ROOT", ".")) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / _LOCK_FILENAME


def _try_lock(handle) -> bool:
    """非阻塞独占锁。Windows 用 msvcrt，POSIX 用 flock。"""
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:  # pragma: no cover - 关闭时会自动释放
        pass


def acquire(path: str | Path | None = None) -> bool:
    """抢占领导者身份。已是领导者时幂等返回 True。"""
    global _handle, _lock_path
    if _handle is not None:
        return True

    target = Path(path) if path else _default_lock_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = open(target, "a+")
    except OSError as exc:
        logger.warning("quant leader lock open failed (%s): %s", target, exc)
        return False

    if not _try_lock(handle):
        handle.close()
        logger.info(
            "Another worker holds the quant leader lock (%s); "
            "this worker serves API only", target,
        )
        return False

    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
    except OSError:  # pragma: no cover - 写 pid 只是便于排查
        pass

    _handle = handle
    _lock_path = target
    logger.info("Acquired quant leader lock: %s (pid=%s)", target, os.getpid())
    return True


def release() -> None:
    """释放领导者身份（进程退出时内核也会自动释放）。"""
    global _handle, _lock_path
    if _handle is None:
        return
    try:
        _unlock(_handle)
    finally:
        try:
            _handle.close()
        except OSError:  # pragma: no cover
            pass
        _handle = None
        _lock_path = None


def is_leader() -> bool:
    return _handle is not None
