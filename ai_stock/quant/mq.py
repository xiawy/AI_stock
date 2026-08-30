"""Redis message queue with SQLite fallback (设计文档 §6).

Redis Key 设计 (§6.1):
- ``mq:queue:{name}``      List  — 普通业务队列, 只存 task_id
- ``mq:delayed``           ZSet  — 延迟任务, score = 可执行时间戳
- ``mq:dead:{name}`        List  — 死信队列
- ``mq:task_meta:{id}`     Hash  — 任务完整元数据
- ``mq:lock:{id}`          String— 任务抢占分布式锁 (TTL 600s)
- ``mq:processing``        ZSet  — processing 中任务 (score=开始时间, 巡检用)

SQLite 降级: ``quant_task`` 表实现同一套接口 — 没有部署 Redis 时整个
闭环依然可用 (单机模式), 生产环境建议 Redis + AOF (§6.2).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .config import (
    LOCK_TTL_SECONDS,
    MQ_BACKEND,
    REDIS_CACHE_PREFIX,
    REDIS_DEAD_PREFIX,
    REDIS_DELAYED_KEY,
    REDIS_IDEMPOTENT_PREFIX,
    REDIS_LOCK_PREFIX,
    REDIS_QUEUE_PREFIX,
    REDIS_TASK_META_PREFIX,
    REDIS_URL,
)
from . import db_ops

logger = logging.getLogger(__name__)

# processing 中任务的 ZSet 索引 (score = 开始时间戳), 超时巡检用
REDIS_PROCESSING_KEY = "mq:processing"

# 任务状态
PENDING = "pending"
DELAYED = "delayed"
PROCESSING = "processing"
DONE = "done"
RETRY = "retry"
FAILED = "failed"
DEAD = "dead"


class TaskAlreadyExists(Exception):
    """幂等键冲突: 同一任务已存在, 重复投递被丢弃."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime | None = None) -> float:
    return (dt or _now()).timestamp()


class MQBackend:
    """消息队列后端抽象 (Redis / SQLite 双实现)."""

    name = "abstract"

    def enqueue(
        self,
        queue_name: str,
        task_type: str,
        payload: dict,
        task_id: str,
        priority: int = 0,
        delay_seconds: int = 0,
        max_attempts: int = 3,
    ) -> str:
        raise NotImplementedError

    def pop(self, queue_name: str, timeout: int = 5, consumer_id: str = "") -> Optional[dict]:
        """阻塞取出一个 pending 任务 (抢占锁), 无任务返回 None."""
        raise NotImplementedError

    def ack(self, task_id: str) -> None:
        """任务成功完成."""
        raise NotImplementedError

    def fail(self, task_id: str, error: str, retry_base_delay: int = 30) -> str:
        """任务失败: 未达最大重试 → 延迟重试; 超限 → 死信. 返回 'retry'/'dead'."""
        raise NotImplementedError

    def get_meta(self, task_id: str) -> Optional[dict]:
        raise NotImplementedError

    def promote_delayed(self) -> int:
        """把到期的延迟任务迁移到对应业务队列, 返回迁移数."""
        raise NotImplementedError

    def sweep_timeouts(self, lock_ttl: int = LOCK_TTL_SECONDS) -> list[dict]:
        """回收 processing 超时任务 (锁已过期 = 消费者死亡), 返回回收列表."""
        raise NotImplementedError

    def renew_lock(self, task_id: str, consumer_id: str = "", ttl: int = LOCK_TTL_SECONDS) -> bool:
        """续期任务抢占锁 (长任务防误回收); SQLite 降级后端无锁返回 False."""
        return False

    def depth(self, queue_name: str) -> int:
        raise NotImplementedError

    def dead_depth(self, queue_name: str) -> int:
        raise NotImplementedError

    def stats(self, queues: list[str]) -> dict:
        return {
            "backend": self.name,
            "queues": {q: self.depth(q) for q in queues},
            "dead": {q: self.dead_depth(q) for q in queues},
        }

    # -- 热缓存 (仅 Redis 后端支持, SQLite 返回 None 让调用方跳过) ----------
    def cache_get(self, key: str) -> Optional[str]:
        return None

    def cache_set(self, key: str, value: str, ttl: int) -> bool:
        return False


# ---------------------------------------------------------------------------
# Redis backend
# ---------------------------------------------------------------------------

class RedisMQBackend(MQBackend):
    """生产环境队列: List 只存 task_id, 完整元数据存 Hash."""

    name = "redis"

    def __init__(self, url: str = REDIS_URL, client=None):
        import redis  # 延迟导入, 未装 redis 包时降级

        self._redis = client or redis.Redis.from_url(
            url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5,
        )
        self._redis.ping()
        # BRPOP 等阻塞命令必须走独立连接池且不设读超时: 客户端
        # socket_timeout 与 brpop 阻塞时长相同会互相竞态, 空闲队列上必然抛
        # "Timeout reading from socket" (redis-py 对阻塞命令建议 socket_timeout=None).
        if client is not None:
            self._blocking = client
        else:
            pool = redis.BlockingConnectionPool.from_url(
                url, decode_responses=True,
                socket_timeout=None, socket_connect_timeout=5,
            )
            self._blocking = redis.Redis(connection_pool=pool)

    # -- helpers -----------------------------------------------------------
    def _meta_key(self, task_id: str) -> str:
        return f"{REDIS_TASK_META_PREFIX}{task_id}"

    def _queue_key(self, queue_name: str) -> str:
        return f"{REDIS_QUEUE_PREFIX}{queue_name}"

    def _lock_key(self, task_id: str) -> str:
        return f"{REDIS_LOCK_PREFIX}{task_id}"

    def _dead_key(self, queue_name: str) -> str:
        return f"{REDIS_DEAD_PREFIX}{queue_name}"

    def _idem_key(self, key: str) -> str:
        return f"{REDIS_IDEMPOTENT_PREFIX}{key}"

    # -- MQBackend API ------------------------------------------------------
    def enqueue(self, queue_name, task_type, payload, task_id,
                priority=0, delay_seconds=0, max_attempts=3) -> str:
        r = self._redis
        if r.exists(self._meta_key(task_id)):
            raise TaskAlreadyExists(task_id)
        now = _now()
        available_at = now + timedelta(seconds=max(delay_seconds, 0))
        meta = {
            "task_id": task_id,
            "queue_name": queue_name,
            "task_type": task_type,
            "payload": json.dumps(payload, ensure_ascii=False, default=str),
            "status": DELAYED if delay_seconds > 0 else PENDING,
            "priority": str(priority),
            "available_at": str(_ts(available_at)),
            "created_at": str(_ts(now)),
            "attempts": "0",
            "max_attempts": str(max_attempts),
            "last_error": "",
            "consumer_id": "",
            "processing_started_at": "0",
        }
        pipe = r.pipeline()
        pipe.hset(self._meta_key(task_id), mapping=meta)
        if delay_seconds > 0:
            pipe.zadd(REDIS_DELAYED_KEY, {task_id: _ts(available_at)})
        else:
            pipe.lpush(self._queue_key(queue_name), task_id)
        pipe.execute()
        logger.debug("Enqueued %s task %s -> %s", task_type, task_id, queue_name)
        return task_id

    def pop(self, queue_name, timeout=5, consumer_id=""):
        r = self._redis
        item = self._blocking.brpop(self._queue_key(queue_name), timeout=timeout)
        if item is None:
            return None
        _, task_id = item
        # 抢占分布式锁; 失败 = 被其他消费者拿走
        if not r.set(self._lock_key(task_id), consumer_id, nx=True, ex=LOCK_TTL_SECONDS):
            return None
        meta = r.hgetall(self._meta_key(task_id))
        if not meta or meta.get("status") in (DONE, DEAD, FAILED):
            r.delete(self._lock_key(task_id))
            return None
        now = _ts()
        r.hset(self._meta_key(task_id), mapping={
            "status": PROCESSING,
            "consumer_id": consumer_id,
            "processing_started_at": str(now),
        })
        r.zadd(REDIS_PROCESSING_KEY, {task_id: now})
        return self._meta_to_task(meta, status=PROCESSING, consumer_id=consumer_id)

    def ack(self, task_id):
        r = self._redis
        pipe = r.pipeline()
        pipe.hset(self._meta_key(task_id), "status", DONE)
        pipe.expire(self._meta_key(task_id), 3600)  # done 元数据 1h 后过期
        pipe.delete(self._lock_key(task_id))
        pipe.zrem(REDIS_PROCESSING_KEY, task_id)
        pipe.execute()

    def renew_lock(self, task_id, consumer_id="", ttl=LOCK_TTL_SECONDS):
        """续期抢占锁: 仅锁仍归本消费者持有时续期, 防止 sweep_timeouts 把
        仍在执行的长任务误判为死亡消费者而重新派发 (重复执行).
        阻塞命令专用连接池上取连接, 避免与普通命令连接串用."""
        conn = self._blocking.connection_pool.get_connection("renew")
        try:
            key = self._lock_key(task_id)
            if consumer_id and conn.get(key) != consumer_id:
                return False  # 锁已被 sweep 回收/被抢占, 不再续期 (任务已被重发)
            conn.expire(key, ttl)
            return True
        except Exception as exc:
            logger.debug("renew_lock failed for %s: %s", task_id, exc)
            return False
        finally:
            self._blocking.connection_pool.release(conn)

    def fail(self, task_id, error, retry_base_delay=30):
        r = self._redis
        meta = r.hgetall(self._meta_key(task_id))
        if not meta:
            return DEAD
        attempts = int(meta.get("attempts", 0)) + 1
        max_attempts = int(meta.get("max_attempts", 3))
        queue_name = meta.get("queue_name", "")
        pipe = r.pipeline()
        pipe.hset(self._meta_key(task_id), mapping={
            "attempts": str(attempts),
            "last_error": str(error)[:2000],
        })
        pipe.delete(self._lock_key(task_id))
        pipe.zrem(REDIS_PROCESSING_KEY, task_id)
        if attempts < max_attempts:
            delay = retry_base_delay * (2 ** (attempts - 1))
            available = _ts() + delay
            pipe.hset(self._meta_key(task_id), mapping={
                "status": RETRY,
                "available_at": str(available),
            })
            pipe.zadd(REDIS_DELAYED_KEY, {task_id: available})
            outcome = RETRY
        else:
            pipe.hset(self._meta_key(task_id), "status", FAILED)
            if queue_name:
                pipe.lpush(self._dead_key(queue_name), task_id)
            outcome = DEAD
        pipe.execute()
        return outcome

    def get_meta(self, task_id):
        meta = self._redis.hgetall(self._meta_key(task_id))
        return self._meta_to_task(meta) if meta else None

    def promote_delayed(self):
        r = self._redis
        due = r.zrangebyscore(REDIS_DELAYED_KEY, "-inf", _ts())
        moved = 0
        for task_id in due:
            meta = r.hgetall(self._meta_key(task_id))
            if not meta or meta.get("status") in (DONE, DEAD, FAILED, PROCESSING):
                r.zrem(REDIS_DELAYED_KEY, task_id)
                continue
            r.lpush(self._queue_key(meta.get("queue_name", "")), task_id)
            r.hset(self._meta_key(task_id), "status", PENDING)
            r.zrem(REDIS_DELAYED_KEY, task_id)
            moved += 1
        return moved

    def sweep_timeouts(self, lock_ttl=LOCK_TTL_SECONDS):
        r = self._redis
        cutoff = _ts() - lock_ttl
        stale = r.zrangebyscore(REDIS_PROCESSING_KEY, "-inf", cutoff)
        recovered = []
        for task_id in stale:
            # 锁已过期且仍在 processing = 消费者死亡
            if r.exists(self._lock_key(task_id)):
                # 锁被续期过 (仍在处理), 仅刷新 processing 索引
                r.zadd(REDIS_PROCESSING_KEY, {task_id: _ts()})
                continue
            meta = r.hgetall(self._meta_key(task_id))
            if not meta:
                r.zrem(REDIS_PROCESSING_KEY, task_id)
                continue
            outcome = self.fail(task_id, "processing timeout (lock expired)")
            recovered.append({"task_id": task_id, "outcome": outcome})
        return recovered

    def depth(self, queue_name):
        return int(self._redis.llen(self._queue_key(queue_name)))

    def dead_depth(self, queue_name):
        return int(self._redis.llen(self._dead_key(queue_name)))

    def cache_get(self, key):
        return self._redis.get(f"{REDIS_CACHE_PREFIX}{key}")

    def cache_set(self, key, value, ttl):
        return bool(self._redis.set(f"{REDIS_CACHE_PREFIX}{key}", value, ex=ttl))

    @staticmethod
    def _meta_to_task(meta: dict, **overrides) -> dict:
        task = {
            "task_id": meta.get("task_id", ""),
            "queue_name": meta.get("queue_name", ""),
            "task_type": meta.get("task_type", ""),
            "payload": json.loads(meta.get("payload") or "{}"),
            "status": meta.get("status", PENDING),
            "priority": int(meta.get("priority", 0) or 0),
            "attempts": int(meta.get("attempts", 0) or 0),
            "max_attempts": int(meta.get("max_attempts", 3) or 3),
            "last_error": meta.get("last_error", ""),
            "consumer_id": meta.get("consumer_id", ""),
        }
        task.update(overrides)
        return task


# ---------------------------------------------------------------------------
# SQLite backend (降级)
# ---------------------------------------------------------------------------

class SQLiteMQBackend(MQBackend):
    """单机降级队列: quant_task 表 + 乐观锁 (UPDATE ... WHERE status='pending')."""

    name = "sqlite"

    def __init__(self):
        from .db import init_quant_db

        init_quant_db()
        # 进程内互斥, 降低 SQLite 写竞争
        import threading

        self._lock = threading.Lock()

    def _model(self):
        from .db_models import QuantTask

        return QuantTask

    def enqueue(self, queue_name, task_type, payload, task_id,
                priority=0, delay_seconds=0, max_attempts=3) -> str:
        from .db import session_scope
        from sqlalchemy import exc as sa_exc

        now = _now()
        available_at = now + timedelta(seconds=max(delay_seconds, 0))
        try:
            with self._lock, session_scope() as s:
                if s.get(self._model(), task_id) is not None:
                    raise TaskAlreadyExists(task_id)
                s.add(self._model()(
                    task_id=task_id,
                    queue_name=queue_name,
                    task_type=task_type,
                    payload_json=json.dumps(payload, ensure_ascii=False, default=str),
                    status=DELAYED if delay_seconds > 0 else PENDING,
                    priority=priority,
                    available_at=available_at,
                    attempts=0,
                    max_attempts=max_attempts,
                ))
                s.commit()
        except sa_exc.IntegrityError:
            raise TaskAlreadyExists(task_id) from None
        return task_id

    def pop(self, queue_name, timeout=5, consumer_id=""):
        from .db import session_scope
        from sqlalchemy import and_, or_

        deadline = time.monotonic() + max(timeout, 0)
        while True:
            with self._lock, session_scope() as s:
                now = _now()
                rows = (
                    s.query(self._model())
                    .filter(
                        self._model().queue_name == queue_name,
                        self._model().status == PENDING,
                        self._model().available_at <= now,
                    )
                    .order_by(
                        self._model().priority.desc(),
                        self._model().created_at.asc(),
                    )
                    .limit(5)
                    .all()
                )
                for row in rows:
                    claimed = (
                        s.query(self._model())
                        .filter(
                            self._model().task_id == row.task_id,
                            self._model().status == PENDING,
                        )
                        .update({
                            "status": PROCESSING,
                            "consumer_id": consumer_id,
                            "processing_started_at": now,
                        })
                    )
                    s.commit()
                    if claimed:
                        return row.to_dict()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(remaining, 0.5))

    def ack(self, task_id):
        from .db import session_scope

        with self._lock, session_scope() as s:
            s.query(self._model()).filter(self._model().task_id == task_id).update({
                "status": DONE,
            })
            s.commit()

    def fail(self, task_id, error, retry_base_delay=30):
        from .db import session_scope

        with self._lock, session_scope() as s:
            row = s.get(self._model(), task_id)
            if row is None:
                return DEAD
            row.attempts += 1
            row.last_error = str(error)[:2000]
            if row.attempts < row.max_attempts:
                delay = retry_base_delay * (2 ** (row.attempts - 1))
                row.status = DELAYED
                row.available_at = _now() + timedelta(seconds=delay)
                outcome = RETRY
            else:
                row.status = DEAD
                outcome = DEAD
            s.commit()
            return outcome

    def get_meta(self, task_id):
        from .db import session_scope

        with session_scope() as s:
            row = s.get(self._model(), task_id)
            return row.to_dict() if row else None

    def promote_delayed(self):
        from .db import session_scope

        with self._lock, session_scope() as s:
            result = (
                s.query(self._model())
                .filter(
                    self._model().status == DELAYED,
                    self._model().available_at <= _now(),
                )
                .update({"status": PENDING}, synchronize_session=False)
            )
            s.commit()
            # SQLAlchemy 2.x: Query.update() 直接返回 int
            return result if isinstance(result, int) else (result.rowcount or 0)

    def sweep_timeouts(self, lock_ttl=LOCK_TTL_SECONDS):
        from .db import session_scope

        cutoff = _now() - timedelta(seconds=lock_ttl)
        recovered = []
        with self._lock, session_scope() as s:
            rows = (
                s.query(self._model())
                .filter(
                    self._model().status == PROCESSING,
                    self._model().processing_started_at < cutoff,
                )
                .all()
            )
            task_ids = []
            for row in rows:
                task_ids.append(row.task_id)
                row.attempts += 1
                row.last_error = "processing timeout (recovered by sweeper)"
                if row.attempts < row.max_attempts:
                    row.status = PENDING
                    row.available_at = _now()
                    outcome = RETRY
                else:
                    row.status = DEAD
                    outcome = DEAD
                recovered.append({"task_id": row.task_id, "outcome": outcome})
            s.commit()
        return recovered

    def renew_lock(self, task_id, consumer_id="", ttl=LOCK_TTL_SECONDS):
        """SQLite 降级后端无独立锁: 把 processing_started_at 刷到当前时刻,
        使 sweep_timeouts 的超时判定以最后续期时刻起算 (与 Redis 锁续期等效)."""
        from .db import session_scope

        with self._lock, session_scope() as s:
            claimed = (
                s.query(self._model())
                .filter(
                    self._model().task_id == task_id,
                    self._model().status == PROCESSING,
                )
                .update({"processing_started_at": _now()})
            )
            s.commit()
            return bool(claimed)

    def depth(self, queue_name):
        return db_ops.count_tasks(PENDING, queue_name) + db_ops.count_tasks(DELAYED, queue_name)

    def dead_depth(self, queue_name):
        return db_ops.count_tasks(DEAD, queue_name)


# ---------------------------------------------------------------------------
# Backend factory & enqueue API
# ---------------------------------------------------------------------------

_backend: Optional[MQBackend] = None
_backend_lock = threading.Lock()


def get_mq_backend(forced: Optional[str] = None) -> MQBackend:
    """获取队列后端单例.

    - ``redis``  → 强制 Redis (连不上抛异常)
    - ``sqlite`` → 强制 SQLite 降级
    - ``auto``   → 尝试 Redis, 失败自动降级 SQLite 并告警 (默认)
    """
    global _backend
    if _backend is not None:
        return _backend
    with _backend_lock:
        if _backend is not None:
            return _backend
        mode = (forced or MQ_BACKEND).lower()
        if mode in ("redis", "auto"):
            try:
                _backend = RedisMQBackend()
                logger.info("Quant MQ backend: redis (%s)", REDIS_URL)
                return _backend
            except Exception as exc:
                if mode == "redis":
                    raise
                logger.warning(
                    "Redis 不可用 (%s), 量化队列降级为 SQLite (单机模式); "
                    "生产环境请部署 Redis 并开启 AOF", exc,
                )
        _backend = SQLiteMQBackend()
        logger.info("Quant MQ backend: sqlite fallback")
        return _backend


def reset_mq_backend() -> None:
    """测试辅助: 重置后端单例."""
    global _backend
    _backend = None


def enqueue_task(
    queue_name: str,
    task_type: str,
    payload: dict | None = None,
    idempotent_key: str | None = None,
    priority: int = 0,
    delay_seconds: int = 0,
    max_attempts: int = 3,
) -> tuple[str, bool]:
    """投递任务 (生产者统一入口).

    idempotent_key 同时作为 task_id — 同 key 重复投递直接丢弃 (幂等,
    §5.3 调度幂等 / §6.4). 返回 (task_id, created: bool).
    """
    backend = get_mq_backend()
    task_id = idempotent_key or f"{task_type}_{uuid.uuid4().hex[:12]}"
    try:
        backend.enqueue(
            queue_name, task_type, payload or {}, task_id,
            priority=priority, delay_seconds=delay_seconds,
            max_attempts=max_attempts,
        )
        return task_id, True
    except TaskAlreadyExists:
        logger.debug("Duplicate task dropped (idempotent): %s", task_id)
        return task_id, False
