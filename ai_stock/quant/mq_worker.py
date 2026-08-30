"""MQ worker threads: consumers, delayed promoter, timeout sweeper.

- ``TaskConsumer``    — 每队列一个消费线程, brpop/轮询 + handler 分发 + 心跳
- ``DelayedWorker``   — 后台线程, 轮询 mq:delayed 迁移到期任务 (§6.4)
- ``TimeoutSweeper``  — 后台线程, 回收 processing 卡死任务 (锁超时, §6.4)
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Callable, Optional

from .config import (
    ALL_QUEUES,
    CONSUME_TIMEOUT,
    DEAD_LETTER_ALERT_THRESHOLD,
    DELAYED_POLL_INTERVAL,
    HEARTBEAT_INTERVAL,
    LOCK_TTL_SECONDS,
    RETRY_BASE_DELAY,
    SWEEP_INTERVAL,
)
from .mq import MQBackend, enqueue_task, get_mq_backend
from . import db_ops

logger = logging.getLogger(__name__)

# handler 签名: (task: dict) -> dict (结果仅写日志/决策记录)
TaskHandler = Callable[[dict], dict]


class TaskConsumer(threading.Thread):
    """队列消费者线程: pop → handler → ack / fail(指数退避重试→死信)."""

    def __init__(
        self,
        queue_name: str,
        handlers: dict[str, TaskHandler],
        backend: Optional[MQBackend] = None,
        name: Optional[str] = None,
        on_dead_letter: Optional[Callable[[dict], None]] = None,
    ):
        super().__init__(name=name or f"consumer-{queue_name}", daemon=True)
        self.queue_name = queue_name
        self.handlers = handlers
        self.backend = backend or get_mq_backend()
        self.consumer_id = f"{queue_name}_{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._beat_counter = 0
        self.on_dead_letter = on_dead_letter

    def stop(self, timeout: float = 6.0) -> None:
        self._stop.set()
        self.join(timeout=timeout)

    def run(self) -> None:
        logger.info("Consumer %s started (queue=%s)", self.consumer_id, self.queue_name)
        db_ops.beat(self.consumer_id, self.queue_name)
        while not self._stop.is_set():
            try:
                task = self.backend.pop(
                    self.queue_name, timeout=CONSUME_TIMEOUT, consumer_id=self.consumer_id,
                )
            except Exception as exc:
                logger.error("MQ pop failed on %s: %s", self.queue_name, exc)
                self._stop.wait(2.0)
                continue
            if task is None:
                self._maybe_beat()
                continue
            self._process(task)
            self._maybe_beat()
        logger.info("Consumer %s stopped", self.consumer_id)

    def _maybe_beat(self) -> None:
        self._beat_counter += 1
        if self._beat_counter >= 3:  # ≈3×CONSUME_TIMEOUT, 低于 HEARTBEAT_INTERVAL 也能覆盖
            self._beat_counter = 0
            db_ops.beat(self.consumer_id, self.queue_name)

    def _process(self, task: dict) -> None:
        task_id = task["task_id"]
        task_type = task.get("task_type", "")
        handler = self.handlers.get(task_type)
        if handler is None:
            logger.error("No handler for task_type=%s (task=%s)", task_type, task_id)
            outcome = self.backend.fail(task_id, f"no handler for {task_type}")
            self._notify_dead(task, outcome)
            return
        # 长任务锁续期: handler 时长可能超过 LOCK_TTL_SECONDS (选股深分析含多轮
        # LLM 调用), 不续期会被 sweep_timeouts 误判为死亡消费者而重复派发.
        renew_stop = threading.Event()
        renew_thread = threading.Thread(
            target=self._renew_loop,
            args=(task_id, renew_stop),
            name=f"lock-renew-{task_id[:24]}",
            daemon=True,
        )
        renew_thread.start()
        try:
            result = handler(task) or {}
            self.backend.ack(task_id)
            logger.debug(
                "Task %s/%s done: %s", task_type, task_id,
                str(result)[:200],
            )
        except Exception as exc:
            logger.warning(
                "Task %s/%s failed: %s (attempt %d/%d)",
                task_type, task_id, exc,
                task.get("attempts", 0) + 1, task.get("max_attempts", 3),
                exc_info=True,
            )
            outcome = self.backend.fail(task_id, str(exc), RETRY_BASE_DELAY)
            self._notify_dead(task, outcome)
        finally:
            renew_stop.set()

    def _renew_loop(self, task_id: str, stop_event: threading.Event) -> None:
        """每 LOCK_TTL/5 续期一次抢占锁, 直到任务结束."""
        interval = max(LOCK_TTL_SECONDS // 5, 1)
        while not stop_event.wait(interval):
            try:
                self.backend.renew_lock(task_id, self.consumer_id)
            except Exception as exc:
                logger.debug("Lock renew failed for %s: %s", task_id, exc)

    def _notify_dead(self, task: dict, outcome: str) -> None:
        if outcome in ("dead", DEAD) and self.on_dead_letter is not None:
            try:
                self.on_dead_letter(task)
            except Exception:
                logger.exception("dead-letter callback failed")


DEAD = "dead"


class DelayedWorker(threading.Thread):
    """延迟任务调度器: 把 mq:delayed 中到期任务迁回业务队列 (sleep 0.2s)."""

    def __init__(self, backend: Optional[MQBackend] = None):
        super().__init__(name="mq-delayed-worker", daemon=True)
        self.backend = backend or get_mq_backend()
        self._stop = threading.Event()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self.join(timeout=timeout)

    def run(self) -> None:
        logger.info("Delayed worker started")
        while not self._stop.is_set():
            try:
                moved = self.backend.promote_delayed()
                if moved:
                    logger.info("Delayed worker promoted %d task(s)", moved)
            except Exception as exc:
                logger.error("Delayed worker error: %s", exc)
            self._stop.wait(DELAYED_POLL_INTERVAL)
        logger.info("Delayed worker stopped")


class TimeoutSweeper(threading.Thread):
    """超时巡检: 扫描 processing 超时任务 (锁过期 = 消费者死亡), 重试或入死信."""

    def __init__(
        self,
        backend: Optional[MQBackend] = None,
        on_dead_letter: Optional[Callable[[dict], None]] = None,
    ):
        super().__init__(name="mq-timeout-sweeper", daemon=True)
        self.backend = backend or get_mq_backend()
        self._stop = threading.Event()
        self.on_dead_letter = on_dead_letter

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self.join(timeout=timeout)

    def run(self) -> None:
        logger.info("Timeout sweeper started (interval=%ds)", SWEEP_INTERVAL)
        while not self._stop.is_set():
            try:
                recovered = self.backend.sweep_timeouts(LOCK_TTL_SECONDS)
                for item in recovered:
                    logger.warning(
                        "Recovered stuck task %s -> %s", item["task_id"], item["outcome"],
                    )
                    if item["outcome"] == DEAD and self.on_dead_letter:
                        try:
                            self.on_dead_letter({"task_id": item["task_id"]})
                        except Exception:
                            logger.exception("dead-letter callback failed")
            except Exception as exc:
                logger.error("Timeout sweeper error: %s", exc)
            self._stop.wait(SWEEP_INTERVAL)
        logger.info("Timeout sweeper stopped")


def default_dead_letter_alert(task: dict) -> None:
    """死信告警 (CRITICAL): 死信出现代表业务异常, 需人工排查 (§6.4)."""
    logger.critical(
        "DEAD LETTER task %s (type=%s) — 业务异常, 请人工排查",
        task.get("task_id"), task.get("task_type", "?"),
    )
    db_ops.log_decision(
        agent="mq_monitor",
        decision="dead_letter",
        reason=f"任务进入死信队列: {task.get('task_id')}",
        detail=task,
    )


def check_dead_letter_backlog(queues: tuple[str, ...] = ALL_QUEUES) -> int:
    """监控用: 各队列死信堆积总量 (超过阈值告警, §14.3)."""
    backend = get_mq_backend()
    total = sum(backend.dead_depth(q) for q in queues)
    if total >= DEAD_LETTER_ALERT_THRESHOLD:
        logger.critical("死信队列堆积 %d 个任务!", total)
    return total
