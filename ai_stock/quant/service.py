"""QuantService — 量化交易子系统一站式装配与生命周期管理.

组装设计文档的全部运行时组件:

1. 初始化 SQLite 业务表 + ``system_config`` 种子 (``global_trade_enable=1``,
   默认允许**模拟盘**下单; 实盘需 ``QUANT_LIVE_MODE=1`` 且人工开闸)
2. 种子默认策略规则与单测用例 (§10.1)
3. 启动五个队列消费者: selection_control / buy / hold / risk / orchestrator
   (§7), risk 队列并入维护任务 handler (日报/清理/超时巡检/到期出池)
4. 启动延迟任务迁移线程 + 超时巡检线程 (§6.4)
5. 重启恢复: Orchestrator 扫描 running flow (§8.1)
6. 启动定时调度层: 回调仅入队 (§5), 并补跑停机/休眠错过的选股槽位

启动入口: ``python -m ai_stock.quant`` 或 ``run_service()``.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from . import db_ops
from .agents import build_handlers
from .config import (
    ALL_QUEUES,
    CONFIG_MIGRATIONS,
    CONFIG_SCHEMA_KEY,
    CONFIG_SCHEMA_VERSION,
    RISK_QUEUE,
    SYSTEM_CONFIG_SEED,
)
from .db import init_quant_db
from .mq import get_mq_backend
from .mq_worker import (
    DelayedWorker,
    TaskConsumer,
    TimeoutSweeper,
    default_dead_letter_alert,
)
from .orchestrator import get_orchestrator
from .rules_engine import seed_rules_and_cases
from .scheduler import QuantScheduler, build_maintenance_handlers, catch_up_selection

logger = logging.getLogger(__name__)


def _seed_system_config() -> None:
    """初始化种子配置 + 执行版本化迁移 (P0-2 / A2).

    语义分两层:
    1. **补缺**: 库里没有的键按 ``SYSTEM_CONFIG_SEED`` 写入;
    2. **迁移**: 默认值变更通过 ``CONFIG_MIGRATIONS`` 下发, 且**只改「值仍是
       旧默认值」的键** —— 运维手工调整过的值绝不动。

    历史教训: 9 月首装的库按 ``global_trade_enable='0'`` 落库, 之后代码把默认
    值改成 ``'1'``, 但「已存在的键一律不覆盖」使新默认值永远到不了存量库, 直接
    导致 6 周 0 成交。没有第 2 层, 下次改任何默认值都会重演。
    """
    existing = db_ops.get_all_config()
    seeded = 0
    for key, (value, description) in SYSTEM_CONFIG_SEED.items():
        if key not in existing:
            db_ops.set_config_value(key, value, description)
            seeded += 1
    if seeded:
        logger.info("system_config 补种 %d 个缺失键", seeded)

    current_version = _read_schema_version(existing)
    if current_version >= CONFIG_SCHEMA_VERSION:
        logger.info(
            "system_config schema v%d 已是最新, global_trade_enable=%s",
            current_version, db_ops.get_config_value("global_trade_enable", "?"),
        )
        return

    applied = _apply_config_migrations(current_version, existing)
    db_ops.set_config_value(
        CONFIG_SCHEMA_KEY, str(CONFIG_SCHEMA_VERSION),
        "配置种子 schema 版本 (默认值变更迁移用)",
    )
    logger.info(
        "system_config 迁移完成: v%d -> v%d, 应用 %d 项 (global_trade_enable=%s)",
        current_version, CONFIG_SCHEMA_VERSION, applied,
        db_ops.get_config_value("global_trade_enable", "?"),
    )


def _read_schema_version(existing: dict[str, str]) -> int:
    """读取当前 schema 版本; 无版本字段时按 v1 处理 (首版种子)."""
    raw = existing.get(CONFIG_SCHEMA_KEY, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        # 老库没有版本标记: 视为 v1 (其中 global_trade_enable 等仍是初版默认值)
        return 1


def _apply_config_migrations(
    from_version: int, existing: dict[str, str],
) -> int:
    """按版本升序执行迁移, 返回实际变更的键数."""
    applied = 0
    for migration in CONFIG_MIGRATIONS:
        if int(migration["version"]) <= from_version:
            continue
        for key, old_defaults, new_value in migration["changes"]:
            current = existing.get(key)
            if current is None:
                # 键不存在 (全新库或新增键): 交给补种层处理
                db_ops.set_config_value(key, new_value)
                applied += 1
                logger.info("config migration add %s=%s (v%s)", key, new_value,
                            migration["version"])
                continue
            if current not in tuple(old_defaults):
                # 运维手工改过 → 尊重人工设置, 不动
                logger.info(
                    "config migration skip %s (current=%s 非旧默认值, 保留人工设置)",
                    key, current,
                )
                continue
            db_ops.set_config_value(key, new_value)
            applied += 1
            logger.warning(
                "config migration applied: %s %s -> %s (v%s: %s)",
                key, current, new_value, migration["version"],
                migration["description"],
            )
    return applied


class QuantService:
    """量化子系统运行时: 消费者 + 后台线程 + 调度器的统一启停."""

    def __init__(self, with_scheduler: bool = True):
        self.with_scheduler = with_scheduler
        self.consumers: list[TaskConsumer] = []
        self.delayed_worker: Optional[DelayedWorker] = None
        self.sweeper: Optional[TimeoutSweeper] = None
        self.scheduler: Optional[QuantScheduler] = None
        self._started = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            init_quant_db()
            _seed_system_config()
            seed_rules_and_cases()

            handlers = build_handlers()
            handlers.setdefault(RISK_QUEUE, {}).update(build_maintenance_handlers())

            backend = get_mq_backend()
            for queue in ALL_QUEUES:
                consumer = TaskConsumer(
                    queue,
                    handlers.get(queue, {}),
                    backend=backend,
                    on_dead_letter=default_dead_letter_alert,
                )
                consumer.start()
                self.consumers.append(consumer)

            self.delayed_worker = DelayedWorker(backend=backend)
            self.delayed_worker.start()
            self.sweeper = TimeoutSweeper(
                backend=backend, on_dead_letter=default_dead_letter_alert,
            )
            self.sweeper.start()

            recovery = get_orchestrator().recover_on_startup()
            if self.with_scheduler:
                self.scheduler = QuantScheduler()
                scheduler_backend = self.scheduler.start()
                # 停机/休眠错过超过 30 分钟宽限窗的选股槽位不会被 cron 补发,
                # 启动时检查并补跑一次 (幂等, 失败不影响启动主链)
                try:
                    catch_up_selection()
                except Exception as exc:
                    logger.warning("Selection catch-up failed: %s", exc)
            else:
                scheduler_backend = "disabled"

            self._started = True
            logger.info(
                "QuantService started: mq=%s, consumers=%d, scheduler=%s, "
                "recovery=%s, global_trade_enable=%s",
                backend.name, len(self.consumers), scheduler_backend,
                {k: len(v) for k, v in recovery.items()},
                db_ops.get_config_value("global_trade_enable", "0"),
            )

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            if self.scheduler is not None:
                self.scheduler.stop()
                self.scheduler = None
            if self.delayed_worker is not None:
                self.delayed_worker.stop()
                self.delayed_worker = None
            if self.sweeper is not None:
                self.sweeper.stop()
                self.sweeper = None
            for consumer in self.consumers:
                consumer.stop()
            self.consumers.clear()
            self._started = False
            logger.info("QuantService stopped")

    @property
    def started(self) -> bool:
        return self._started

    # ------------------------------------------------------------------
    # 运维视图 (§14.3)
    # ------------------------------------------------------------------

    def status(self) -> dict:
        backend = get_mq_backend()
        stats = backend.stats(list(ALL_QUEUES))
        return {
            "started": self._started,
            "mq_backend": backend.name,
            "queues": stats,
            "scheduler": self.scheduler.backend_name if self.scheduler else "disabled",
            "global_trade_enable": db_ops.get_config_value("global_trade_enable", "?"),
            "trade_frozen": db_ops.get_config_value("trade_frozen", "0"),
            "optional_pool_active": len(db_ops.get_optional_pool("active")),
            "holdings": len(db_ops.get_holdings()),
            "flows_running": len(db_ops.get_running_flows()),
            "stale_consumers": len(db_ops.get_stale_consumers()),
        }


_service: Optional[QuantService] = None
_service_lock = threading.Lock()


def get_quant_service(with_scheduler: bool = True) -> QuantService:
    """全局 QuantService 单例."""
    global _service
    with _service_lock:
        if _service is None:
            _service = QuantService(with_scheduler=with_scheduler)
        return _service


def run_service(with_scheduler: bool = True, block: bool = True) -> QuantService:
    """启动服务; ``block=True`` 时阻塞直到 KeyboardInterrupt (进程入口用)."""
    service = get_quant_service(with_scheduler=with_scheduler)
    service.start()
    if block:
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            logger.info("Interrupt received, shutting down QuantService...")
            service.stop()
    return service
