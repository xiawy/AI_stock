"""Three-tier cache + data-source circuit breaker (设计文档 §9).

CacheManager 读取顺序: 内存 LRU → Redis 热缓存 → 外部数据源;
回写 内存 + Redis, 可选写 SQLite 冷归档 (Redis 故障可从 SQLite 重建).
``allow_fallback_stale``: 熔断场景允许返回过期缓存兜底.

CircuitBreaker: 单个数据源连续 N 次失败打开熔断; 熔断窗口期不再请求
外部接口, 直接返回缓存兜底; half-open 恢复试探.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from datetime import timedelta
from typing import Any, Callable, Optional

from .config import (
    CIRCUIT_FAILURE_THRESHOLD,
    CIRCUIT_OPEN_SECONDS,
    MEMORY_CACHE_MAXSIZE,
)
from . import db_ops

logger = logging.getLogger(__name__)


class CircuitOpenError(Exception):
    """数据源熔断打开中, 请求被拒绝."""


class CircuitBreaker:
    """三态熔断器: closed → open (连续失败) → half-open (窗口到期试探)."""

    def __init__(
        self,
        source_name: str,
        failure_threshold: int = CIRCUIT_FAILURE_THRESHOLD,
        open_seconds: int = CIRCUIT_OPEN_SECONDS,
    ):
        self.source_name = source_name
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self._lock = threading.Lock()
        self._state = "closed"
        self._failure_count = 0
        self._opened_at = 0.0

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == "open":
                # 窗口到期 → half-open (允许一次试探)
                if time.monotonic() - self._opened_at >= self.open_seconds:
                    self._state = "half_open"
            return self._state

    def allow(self) -> bool:
        """熔断关闭/半开时允许请求."""
        return self.state in ("closed", "half_open")

    def record_success(self) -> None:
        with self._lock:
            if self._state != "closed":
                logger.info("Circuit %s recovered → closed", self.source_name)
            self._state = "closed"
            self._failure_count = 0
            db_ops.circuit_set(self.source_name, "closed", 0)

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            if self._state == "half_open" or self._failure_count >= self.failure_threshold:
                self._state = "open"
                self._opened_at = time.monotonic()
                logger.warning(
                    "Circuit %s OPEN (failures=%d) — %ds 内直接走缓存兜底",
                    self.source_name, self._failure_count, self.open_seconds,
                )
                db_ops.circuit_set(self.source_name, "open", self._failure_count)

    def call(self, fn: Callable[[], Any], *, fallback: Any = None) -> Any:
        """带熔断的调用: 打开时直接抛 CircuitOpenError (或返回 fallback)."""
        if not self.allow():
            if fallback is not None:
                return fallback
            raise CircuitOpenError(self.source_name)
        try:
            result = fn()
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result


class CacheManager:
    """三级缓存: 内存 LRU → Redis 热缓存 → SQLite 归档 (读穿透)."""

    def __init__(self, redis_cache=None):
        # redis_cache: 提供 cache_get/cache_set 的对象 (MQBackend 即满足)
        self._redis = redis_cache
        self._mem: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._mem_lock = threading.Lock()

    # -- L1 内存 LRU --------------------------------------------------------
    def _mem_get(self, key: str) -> Any | None:
        with self._mem_lock:
            entry = self._mem.get(key)
            if entry is None:
                return None
            expires, value = entry
            if expires < time.time():
                del self._mem[key]
                return None
            self._mem.move_to_end(key)
            return value

    def _mem_set(self, key: str, value: Any, ttl: int) -> None:
        with self._mem_lock:
            self._mem[key] = (time.time() + ttl, value)
            self._mem.move_to_end(key)
            while len(self._mem) > MEMORY_CACHE_MAXSIZE:
                self._mem.popitem(last=False)

    # -- 统一入口 ------------------------------------------------------------
    def get(self, key: str) -> Any | None:
        """纯读 (不触发回源): L1 → Redis → SQLite 归档."""
        value = self._mem_get(key)
        if value is not None:
            return value
        if self._redis is not None:
            try:
                raw = self._redis.cache_get(key)
                if raw is not None:
                    return json.loads(raw)
            except Exception as exc:
                logger.debug("Redis cache read failed (%s); skip tier-2", exc)
        return db_ops.cache_get(key)

    def set(self, key: str, value: Any, ttl: int, category: str = "",
            archive: bool = True) -> None:
        """写 L1 + Redis (+ 可选 SQLite 冷归档)."""
        self._mem_set(key, value, ttl)
        if self._redis is not None:
            try:
                self._redis.cache_set(
                    key, json.dumps(value, ensure_ascii=False, default=str), ttl,
                )
            except Exception as exc:
                logger.debug("Redis cache write failed (%s); skip tier-2", exc)
        if archive:
            try:
                db_ops.cache_put(key, value, category=category, ttl_seconds=ttl)
            except Exception as exc:
                logger.debug("SQLite cache archive failed (%s)", exc)

    def get_or_fetch(
        self,
        key: str,
        fetch_fn: Callable[[], Any],
        ttl: int,
        category: str = "",
        breaker: Optional[CircuitBreaker] = None,
        allow_fallback_stale: bool = False,
    ) -> Any:
        """读缓存, miss 时回源; 熔断/回源失败时允许返回过期缓存兜底 (§9.1)."""
        cached = self.get(key)
        if cached is not None:
            return cached

        try:
            if breaker is not None:
                value = breaker.call(fetch_fn)
            else:
                value = fetch_fn()
        except Exception as exc:
            logger.warning(
                "Fetch failed for %s (%s); %s",
                key, exc,
                "trying stale archive" if allow_fallback_stale else "no fallback",
            )
            if allow_fallback_stale:
                stale = self._get_stale(key)
                if stale is not None:
                    logger.warning("Using STALE cached data for %s (degraded)", key)
                    return stale
            raise

        if value is None:
            return None  # 外部接口返回 None 不向上传, 由调用方处理
        self.set(key, value, ttl, category=category)
        return value

    def _get_stale(self, key: str) -> Any | None:
        """忽略过期时间读 SQLite 归档 (仅降级兜底时使用)."""
        from .db import session_scope
        from .db_models import QuantCacheData

        try:
            with session_scope() as s:
                row = s.get(QuantCacheData, key)
                return json.loads(row.payload) if row else None
        except Exception:
            return None


# 模块级单例 (惰性绑定 redis)
_manager: Optional[CacheManager] = None
_manager_lock = threading.Lock()
_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def get_cache_manager(redis_cache=None) -> CacheManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = CacheManager(redis_cache=redis_cache)
        return _manager


def get_breaker(source_name: str) -> CircuitBreaker:
    with _breakers_lock:
        if source_name not in _breakers:
            _breakers[source_name] = CircuitBreaker(source_name)
        return _breakers[source_name]
