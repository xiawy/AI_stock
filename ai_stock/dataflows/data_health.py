"""数据源健康矩阵 + 失败退避 (P0-1 / A1).

背景: 东财 ``push2*.eastmoney.com`` 的 ``clist/get``（板块列表）集群对采集机 IP
做了应用层封禁。原 ``DataSourceCircuit`` 只记录「失败次数」，无法区分
「临时超时 / 被反爬封禁 / 接口下线」，导致选股上游 94% 空转持续 6 周无人发现。

本模块提供两件事：

1. **健康矩阵**：按 ``(source, api)`` 记录调用成功/失败、延迟样本、
   最近成功时间与连续失败次数，输出成功率 / P95 延迟，供日报与 SLO 告警消费。
2. **指数退避**：连续失败达到阈值后，对同一 ``(source)`` 进入
   ``5min → 15min → 60min`` 退避窗口。退避期间调用方直接跳过主源走备用源，
   不再每 10 分钟硬撞一次（硬撞只会加重封禁）。

设计原则：纯内存、无锁竞争热点（一把粗锁足够，调用频率远低于 1/秒）、
任何异常都不得影响取数主链。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# 每 (source, api) 保留的延迟样本数
_LATENCY_WINDOW = 50

_lock = threading.Lock()
# key -> {"ok": int, "fail": int, "latencies": [ms], "last_ok": ts|None,
#         "last_error": str, "consecutive_fail": int, "backoff_until": ts}
_stats: dict[tuple[str, str], dict] = {}


def _bucket(source: str, api: str) -> dict:
    key = (source or "unknown", api or "default")
    bucket = _stats.get(key)
    if bucket is None:
        bucket = {
            "ok": 0,
            "fail": 0,
            "latencies": [],
            "last_ok": None,
            "last_error": "",
            "consecutive_fail": 0,
            "backoff_until": 0.0,
        }
        _stats[key] = bucket
    return bucket


def record_ok(source: str, api: str, latency_ms: float) -> None:
    """记录一次成功调用 (清空连续失败与退避)。"""
    with _lock:
        bucket = _bucket(source, api)
        bucket["ok"] += 1
        bucket["consecutive_fail"] = 0
        bucket["backoff_until"] = 0.0
        bucket["last_ok"] = time.time()
        bucket["latencies"].append(max(0.0, float(latency_ms)))
        if len(bucket["latencies"]) > _LATENCY_WINDOW:
            del bucket["latencies"][: len(bucket["latencies"]) - _LATENCY_WINDOW]


def record_fail(
    source: str,
    api: str,
    error: str = "",
    backoff_steps: tuple[int, ...] = (300, 900, 3600),
    threshold: int = 3,
) -> bool:
    """记录一次失败; 达到阈值后进入指数退避。返回是否已进入退避。"""
    now = time.time()
    with _lock:
        bucket = _bucket(source, api)
        bucket["fail"] += 1
        bucket["consecutive_fail"] += 1
        bucket["last_error"] = str(error)[:300]
        streak = bucket["consecutive_fail"]
        if streak < threshold:
            return False
        step_idx = min(streak - threshold, len(backoff_steps) - 1)
        seconds = backoff_steps[step_idx]
        bucket["backoff_until"] = now + seconds
        return True


def in_backoff(source: str, api: str = "") -> float:
    """返回剩余退避秒数 (0 表示不在退避中).

    ``api`` 为空时按 source 下任意接口的最大剩余退避判断 —— 同一主机被封时
    其所有接口应一起退避。
    """
    now = time.time()
    with _lock:
        if api:
            bucket = _stats.get((source, api))
            if not bucket:
                return 0.0
            return max(0.0, bucket["backoff_until"] - now)
        remaining = 0.0
        for (src, _api), bucket in _stats.items():
            if src != source:
                continue
            remaining = max(remaining, bucket["backoff_until"] - now)
        return max(0.0, remaining)


def reset(source: Optional[str] = None) -> None:
    """清空统计 (source=None 时全清); 测试与人工恢复用。"""
    with _lock:
        if source is None:
            _stats.clear()
        else:
            for key in [k for k in _stats if k[0] == source]:
                _stats.pop(key, None)


def _p95(samples: list[float]) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return round(ordered[idx], 1)


def snapshot() -> list[dict]:
    """健康矩阵快照: 每 (源, 接口) 一行, 供日报/告警/接口展示。"""
    now = time.time()
    rows: list[dict] = []
    with _lock:
        items = list(_stats.items())
    for (source, api), bucket in items:
        total = bucket["ok"] + bucket["fail"]
        rows.append({
            "source": source,
            "api": api,
            "calls": total,
            "success_rate": round(bucket["ok"] / total, 3) if total else None,
            "p95_latency_ms": _p95(bucket["latencies"]),
            "last_success": bucket["last_ok"],
            "seconds_since_success": (
                round(now - bucket["last_ok"], 1) if bucket["last_ok"] else None
            ),
            "consecutive_fail": bucket["consecutive_fail"],
            "backoff_seconds": max(0.0, round(bucket["backoff_until"] - now, 1)),
            "last_error": bucket["last_error"],
        })
    return sorted(rows, key=lambda r: (r["source"], r["api"]))


def degraded_sources(min_calls: int = 3, threshold: float = 0.5) -> list[dict]:
    """成功率跌破阈值的源 (至少 ``min_calls`` 次调用才判定)。"""
    return [
        row for row in snapshot()
        if row["calls"] >= min_calls
        and row["success_rate"] is not None
        and row["success_rate"] < threshold
    ]
