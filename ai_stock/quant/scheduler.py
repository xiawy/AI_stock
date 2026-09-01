"""Task scheduling layer (设计文档 §5).

核心约束 (§5.1): 所有定时回调只做一件事 — 调用 ``enqueue_task()`` 投递任务
到队列, 禁止在回调中执行行情拉取、LLM、计算等耗时逻辑, 避免调度线程
阻塞、定时漂移丢失。

- 交易日历 (§5.2): 非交易日直接跳过入队; 买入/持仓扫描额外要求处于交易时段
- 调度幂等 (§5.3): 每个任务携带对齐到扫描周期的 idempotent_key
- APScheduler 未安装时降级为内置轮询调度线程 (单机模式依然闭环)

轻量维护任务 (日报/清理/超时巡检/到期出池) 也通过入队走 risk 队列消费,
保持「调度只入队」的约束 (§14.4 / §9.2 / §9.3 / §8.1).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time as dtime
from typing import Callable, Optional

from .calendar_utils import get_trade_date, is_trading_day, is_trading_time
from .config import (
    BUY_QUEUE,
    BUY_SCAN_INTERVAL_MINUTES,
    DAILY_REPORT_AT,
    HOLD_QUEUE,
    HOLD_SCAN_INTERVAL_MINUTES,
    RISK_QUEUE,
    RISK_SCAN_INTERVAL_MINUTES,
    SELECTION_QUEUE,
    SELECTION_SCHEDULE,
)
from .mq import enqueue_task

logger = logging.getLogger(__name__)


def _aligned_hhmm(now: datetime, interval_minutes: int) -> str:
    """把当前时间对齐到扫描周期槽位 (幂等窗口), 返回 HHMM."""
    slot = (now.minute // interval_minutes) * interval_minutes
    return f"{now.hour:02d}{slot:02d}"


def _shifted_time(hour: int, minute: int, delta_minutes: int = 0) -> dtime:
    """构造 daily 触发时刻: 分钟偏移进位, 避免 minute ≥60 时 dtime 抛异常."""
    total = hour * 60 + minute + delta_minutes
    return dtime((total // 60) % 24, total % 60)


# ---------------------------------------------------------------------------
# 触发器 — 只入队, 不做任何业务逻辑 (§5.1)
# ---------------------------------------------------------------------------

def trigger_selection(now: Optional[datetime] = None, force: bool = False) -> Optional[str]:
    """挑选自选触发 (每日 7:00/12:00/18:00): 入队 selection_start."""
    now = now or datetime.now()
    if not force and not is_trading_day(now):
        return None
    trade_date = get_trade_date()
    hhmm = now.strftime("%H%M")
    task_id, created = enqueue_task(
        SELECTION_QUEUE,
        "selection_start",
        payload={"trade_date": trade_date, "trigger": hhmm},
        idempotent_key=f"selection_{trade_date}_{hhmm}",
        priority=2,
    )
    return task_id if created else None


def catch_up_selection(now: Optional[datetime] = None) -> Optional[str]:
    """启动/唤醒补跑 (§5.3 补充): 今天首槽已过点但行业榜尚未产出时,
    补入队一次选股。

    场景: 服务停机/机器休眠超过 APScheduler 的 30 分钟宽限窗后, 错过的
    cron 不会自动补发 → 行业榜/自选池整槽停更。本函数轻量幂等,
    除服务启动时调用外, 还注册为 10 分钟周期 job 兼顾长时间休眠唤醒。
    判据用「今日行业榜是否已产出」而非槽位 flow_id: 补跑/手动触发的
    flow_id 携带触发时刻而非槽位时刻, 按槽位查会重复补发; 而流程超时熔断时
    行业榜不会落库, 榜缺失即代表当日数据确实未产出, 应重试。
    """
    now = now or datetime.now()
    if not is_trading_day(now):
        return None
    from . import db_ops

    first_hour, first_minute = (int(p) for p in SELECTION_SCHEDULE[0].split(":"))
    if (now.hour, now.minute) < (first_hour, first_minute):
        return None  # 首个槽位未到点, 盘前数据尚旧, 不提前跑
    if db_ops.get_running_flows("selection"):
        return None  # 已有选股流程在跑, 避免并行重复执行
    trade_date = get_trade_date()
    if db_ops.get_industry_board_by_date(trade_date):
        return None  # 今日行业榜已产出 (cron/手动/先前补跑)
    task_id = trigger_selection(now=now)
    if task_id:
        logger.info(
            "Selection catch-up enqueued for %s (no board today)", trade_date,
        )
    return task_id


def trigger_buy_scan(now: Optional[datetime] = None, force: bool = False) -> Optional[str]:
    """自选买入扫描 (开盘期间每 30 分钟): 入队 buy_scan."""
    now = now or datetime.now()
    if not force and not is_trading_time(now):
        return None
    window = now.strftime("%Y%m%d") + _aligned_hhmm(now, BUY_SCAN_INTERVAL_MINUTES)
    task_id, created = enqueue_task(
        BUY_QUEUE,
        "buy_scan",
        payload={"window": window},
        idempotent_key=f"buyscan_{window}",
        priority=1,
    )
    return task_id if created else None


def trigger_hold_scan(now: Optional[datetime] = None, force: bool = False) -> Optional[str]:
    """持仓维护扫描 (开盘期间每 10 分钟): 入队 hold_scan."""
    now = now or datetime.now()
    if not force and not is_trading_time(now):
        return None
    window = now.strftime("%Y%m%d") + _aligned_hhmm(now, HOLD_SCAN_INTERVAL_MINUTES)
    task_id, created = enqueue_task(
        HOLD_QUEUE,
        "hold_scan",
        payload={"window": window},
        idempotent_key=f"holdscan_{window}",
        priority=1,
    )
    return task_id if created else None


def trigger_risk_scan(now: Optional[datetime] = None, force: bool = False) -> Optional[str]:
    """异步风控兜底扫描 (每 5 分钟): 入队 risk_scan."""
    now = now or datetime.now()
    if not force and not is_trading_day(now):
        return None
    window = now.strftime("%Y%m%d") + _aligned_hhmm(now, RISK_SCAN_INTERVAL_MINUTES)
    task_id, created = enqueue_task(
        RISK_QUEUE,
        "risk_scan",
        payload={"window": window},
        idempotent_key=f"riskscan_{window}",
        priority=1,
    )
    return task_id if created else None


def trigger_flow_timeout_check(now: Optional[datetime] = None) -> Optional[str]:
    """flow 超时熔断巡检 (每 5 分钟, §8.1): 入队 flow_timeout_check."""
    now = now or datetime.now()
    window = now.strftime("%Y%m%d") + _aligned_hhmm(now, RISK_SCAN_INTERVAL_MINUTES)
    task_id, created = enqueue_task(
        RISK_QUEUE,
        "flow_timeout_check",
        payload={"window": window},
        idempotent_key=f"flowchk_{window}",
        priority=0,
    )
    return task_id if created else None


def trigger_daily_report(now: Optional[datetime] = None) -> Optional[str]:
    """每日运行报告 (§14.4): 收盘后入队, 由 risk 队列消费者生成."""
    now = now or datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    task_id, created = enqueue_task(
        RISK_QUEUE,
        "daily_report",
        payload={"trade_date": date_str},
        idempotent_key=f"daily_report_{date_str}",
        priority=0,
    )
    return task_id if created else None


def trigger_pool_expire(now: Optional[datetime] = None) -> Optional[str]:
    """自选池观察期到期出池 (§7.1.4): 每日收盘后入队."""
    now = now or datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    task_id, created = enqueue_task(
        RISK_QUEUE,
        "pool_expire",
        payload={"trade_date": date_str},
        idempotent_key=f"pool_expire_{date_str}",
        priority=0,
    )
    return task_id if created else None


def trigger_cleanup(now: Optional[datetime] = None) -> Optional[str]:
    """凌晨维护: 缓存归档清理 + 新闻向量过期清理 + 元数据备份 (§9.2/§9.3)."""
    now = now or datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    task_id, created = enqueue_task(
        RISK_QUEUE,
        "maintenance_cleanup",
        payload={"trade_date": date_str},
        idempotent_key=f"cleanup_{date_str}",
        priority=0,
    )
    return task_id if created else None


# ---------------------------------------------------------------------------
# 维护任务 handlers (注册到 risk 队列消费者)
# ---------------------------------------------------------------------------

def handle_daily_report(task: dict) -> dict:
    from . import db_ops
    from .ops_monitor import generate_daily_report

    trade_date = task.get("payload", {}).get("trade_date")
    db_ops.expire_stale_optional()  # 报告前先到期出池
    report = generate_daily_report(trade_date)
    return {"date": report.get("date"), "report_file": report.get("report_file")}


def handle_pool_expire(task: dict) -> dict:
    from . import db_ops

    expired = db_ops.expire_stale_optional()
    return {"expired": expired}


def handle_maintenance_cleanup(task: dict) -> dict:
    from . import db_ops
    from .news_store import get_news_store

    removed_cache = db_ops.cache_cleanup()
    store = get_news_store()
    removed_news = store.cleanup()
    backup_path = None
    try:
        backup_path = store.backup_metadata()
    except Exception as exc:
        logger.warning("News metadata backup failed: %s", exc)
    return {
        "cache_rows_removed": removed_cache,
        "news_expired": removed_news,
        "news_backup": backup_path,
    }


def handle_flow_timeout_check(task: dict) -> dict:
    from .mq_worker import check_dead_letter_backlog
    from .orchestrator import get_orchestrator

    timed_out = get_orchestrator().check_flow_timeouts()
    dead_total = check_dead_letter_backlog()
    return {"timed_out_flows": timed_out, "dead_letter_total": dead_total}


def build_maintenance_handlers() -> dict[str, Callable[[dict], dict]]:
    """维护类任务 handler 表 (并入 risk 队列消费者)."""
    return {
        "daily_report": handle_daily_report,
        "pool_expire": handle_pool_expire,
        "maintenance_cleanup": handle_maintenance_cleanup,
        "flow_timeout_check": handle_flow_timeout_check,
    }


# ---------------------------------------------------------------------------
# 调度器封装: APScheduler 优先, 缺失时降级为内置轮询线程
# ---------------------------------------------------------------------------

class _FallbackScheduler(threading.Thread):
    """无 APScheduler 时的极简调度: 30s 轮询, 日期去重 + 间隔去重."""

    TICK_SECONDS = 30

    def __init__(self, jobs: list[dict]):
        super().__init__(name="quant-fallback-scheduler", daemon=True)
        self.jobs = jobs  # [{kind: 'daily'|'interval', ...}]
        self._stop = threading.Event()
        self._fired_daily: dict[str, str] = {}
        self._last_interval: dict[str, float] = {}

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self.join(timeout=timeout)

    def run(self) -> None:
        logger.info("Fallback scheduler started (%d jobs)", len(self.jobs))
        while not self._stop.is_set():
            now = datetime.now()
            for job in self.jobs:
                try:
                    self._maybe_fire(job, now)
                except Exception as exc:
                    logger.error("Fallback job %s failed: %s", job.get("name"), exc)
            self._stop.wait(self.TICK_SECONDS)

    def _maybe_fire(self, job: dict, now: datetime) -> None:
        name = job["name"]
        if job["kind"] == "daily":
            at: dtime = job["at"]
            today = now.strftime("%Y-%m-%d")
            if (
                (now.hour, now.minute) >= (at.hour, at.minute)
                and self._fired_daily.get(name) != today
            ):
                self._fired_daily[name] = today
                job["fn"]()
        else:  # interval (分钟)
            import time

            interval = job["minutes"] * 60
            last = self._last_interval.get(name)
            if last is None:
                # 启动首拍不立即触发, 避免进程重启后周期任务立即双发
                # (幂等 key 可去重, 但调度节奏应与周期对齐)
                self._last_interval[name] = time.monotonic()
            elif time.monotonic() - last >= interval:
                self._last_interval[name] = time.monotonic()
                job["fn"]()


class QuantScheduler:
    """量化定时调度层: 回调仅入队 (§5.1), 非交易日跳过 (§5.2)."""

    def __init__(self):
        self._aps = None
        self._fallback: Optional[_FallbackScheduler] = None

    @property
    def backend_name(self) -> str:
        if self._aps is not None:
            return "apscheduler"
        if self._fallback is not None:
            return "fallback"
        return "stopped"

    def start(self) -> str:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler

            self._aps = BackgroundScheduler()
            self._register_apscheduler_jobs()
            self._aps.start()
            logger.info("Quant scheduler started (APScheduler)")
            return "apscheduler"
        except ImportError:
            logger.warning(
                "APScheduler 未安装, 调度层降级为内置轮询线程; "
                "建议 pip install 'apscheduler>=3.10'"
            )
            self._fallback = _FallbackScheduler(self._fallback_jobs())
            self._fallback.start()
            return "fallback"

    def stop(self) -> None:
        if self._aps is not None:
            try:
                self._aps.shutdown(wait=False)
            except Exception:
                logger.exception("APScheduler shutdown failed")
            self._aps = None
        if self._fallback is not None:
            self._fallback.stop()
            self._fallback = None

    # ------------------------------------------------------------------
    # Job 定义 (两种后端共用)
    # ------------------------------------------------------------------

    def _fallback_jobs(self) -> list[dict]:
        jobs: list[dict] = []
        for hhmm in SELECTION_SCHEDULE:
            hour, minute = hhmm.split(":")
            jobs.append({
                "name": f"selection_{hhmm}",
                "kind": "daily",
                "at": dtime(int(hour), int(minute)),
                "fn": trigger_selection,
            })
        jobs += [
            {"name": "buy_scan", "kind": "interval",
             "minutes": BUY_SCAN_INTERVAL_MINUTES, "fn": trigger_buy_scan},
            {"name": "hold_scan", "kind": "interval",
             "minutes": HOLD_SCAN_INTERVAL_MINUTES, "fn": trigger_hold_scan},
            {"name": "risk_scan", "kind": "interval",
             "minutes": RISK_SCAN_INTERVAL_MINUTES, "fn": trigger_risk_scan},
            {"name": "flow_timeout_check", "kind": "interval",
             "minutes": RISK_SCAN_INTERVAL_MINUTES, "fn": trigger_flow_timeout_check},
            {"name": "daily_report", "kind": "daily",
             "at": dtime(*DAILY_REPORT_AT), "fn": trigger_daily_report},
            {"name": "pool_expire", "kind": "daily",
             "at": _shifted_time(*DAILY_REPORT_AT, delta_minutes=5), "fn": trigger_pool_expire},
            {"name": "cleanup", "kind": "daily", "at": dtime(3, 0), "fn": trigger_cleanup},
            {"name": "selection_catch_up", "kind": "interval",
             "minutes": 10, "fn": catch_up_selection},
        ]
        return jobs

    def _register_apscheduler_jobs(self) -> None:
        aps = self._aps
        # 触发器只入队且带幂等 key (§5.1/§5.3), 迟到补跑安全;
        # 默认 misfire_grace_time=1s 会在休眠唤醒/线程瞬间饥饿时大量告警并跳过扫描,
        # 这里按周期容忍延迟补跑, 多个错过周期合并为一次 (天然幂等).
        grace = {
            "misfire_grace_time": 30 * 60,
            "coalesce": True,
            "max_instances": 1,
        }
        _pool_expire_at = _shifted_time(*DAILY_REPORT_AT, delta_minutes=5)
        for hhmm in SELECTION_SCHEDULE:
            hour, minute = hhmm.split(":")
            aps.add_job(
                trigger_selection, "cron", hour=int(hour), minute=int(minute),
                id=f"selection_{hhmm}", replace_existing=True, **grace,
            )
        aps.add_job(
            trigger_buy_scan, "interval", minutes=BUY_SCAN_INTERVAL_MINUTES,
            id="buy_scan", replace_existing=True, **grace,
        )
        aps.add_job(
            trigger_hold_scan, "interval", minutes=HOLD_SCAN_INTERVAL_MINUTES,
            id="hold_scan", replace_existing=True, **grace,
        )
        aps.add_job(
            trigger_risk_scan, "interval", minutes=RISK_SCAN_INTERVAL_MINUTES,
            id="risk_scan", replace_existing=True, **grace,
        )
        aps.add_job(
            trigger_flow_timeout_check, "interval", minutes=RISK_SCAN_INTERVAL_MINUTES,
            id="flow_timeout_check", replace_existing=True, **grace,
        )
        aps.add_job(
            trigger_daily_report, "cron", hour=DAILY_REPORT_AT[0],
            minute=DAILY_REPORT_AT[1], id="daily_report", replace_existing=True, **grace,
        )
        aps.add_job(
            trigger_pool_expire, "cron", hour=_pool_expire_at.hour,
            minute=_pool_expire_at.minute, id="pool_expire", replace_existing=True, **grace,
        )
        aps.add_job(
            trigger_cleanup, "cron", hour=3, minute=0,
            id="maintenance_cleanup", replace_existing=True, **grace,
        )
        # 选股补跑巡检: 轻量幂等 (查行业榜是否已产出), 兼顾长时间休眠唤醒后
        # 错过超过 30 分钟宽限窗的槽位 (见 catch_up_selection)
        aps.add_job(
            catch_up_selection, "interval", minutes=10,
            id="selection_catch_up", replace_existing=True, **grace,
        )

