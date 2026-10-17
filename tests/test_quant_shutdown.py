"""量化后台线程的停机收敛（回归：Thread._stop 被 Event 覆盖）。

历史 Bug：``TaskConsumer`` / ``DelayedWorker`` / ``TimeoutSweeper`` /
``_FallbackScheduler`` 都把停止标志命名为 ``self._stop``，而
``threading.Thread`` 内部正好有同名方法 ``_stop()``。覆盖后 ``join()`` 在收尾
（``_wait_for_tstate_lock``）调用 ``self._stop()`` 时抛
``'Event' object is not callable``，导致 ``QuantService.stop()`` 中断 ——
消费者、清扫器都没停干净，Redis 阻塞连接与锁残留，日志只在停机时给出一句
"Quant service stop failed"。

这里同时守住两件事：
1. 源码里不再出现 ``self._stop``（防止回退）；
2. 降级调度线程能 start → stop 干净退出。
"""

from __future__ import annotations

import inspect

import pytest


@pytest.mark.unit
class TestWorkerShutdown:
    def test_no_thread_private_stop_shadowing(self):
        from ai_stock.quant import mq_worker, scheduler

        for module in (mq_worker, scheduler):
            source = inspect.getsource(module)
            assert "self._stop =" not in source, (
                f"{module.__name__} 覆盖了 threading.Thread._stop；"
                "请使用 self._stop_event"
            )
            assert "self._stop." not in source

    def test_fallback_scheduler_stops_cleanly(self):
        from ai_stock.quant.scheduler import _FallbackScheduler

        sched = _FallbackScheduler([])
        sched.start()
        sched.stop(timeout=5.0)      # 修复前这里会抛 'Event' object is not callable
        assert sched.is_alive() is False

    def test_quant_service_stop_is_idempotent_when_not_started(self):
        from ai_stock.quant.service import QuantService

        svc = QuantService(with_scheduler=False)
        svc.stop()                   # 未启动时 stop 直接返回, 不抛
        assert svc.started is False
