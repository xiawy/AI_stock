"""统一多源采集框架: 多源采集 → 归一化 → 合并去重 → 返回.

免费数据源(直连 HTTP)各有失效风险——某个接口挂了不应让整条数据链路
报废。本模块把"数据端点"与"数据来源"解耦:

- 每个端点声明若干**数据源** ``(name, fetch_fn)``; ``fetch_fn`` 负责
  **归一化**, 输出符合该端点统一 schema 的记录列表;
- ``collect_sources``: 逐源采集, 单源异常被隔离(记录错误、继续下一源);
- ``merge_records``: 按业务键合并去重, 排前的源优先级高, 记录带 ``_source``
  溯源标记;
- ``first_success``: 主机级容灾(同一接口的多个镜像域名), 返回第一个成功源.

设计约束:
- 串行采集(免费源多有反爬, 并发只会触发封禁; 东财还有全局节流);
- 框架不做重试——失败源直接降级, 由备用源兜底, 避免把慢失败放大成超时雪崩.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# 数据源: (名称, 采集函数); fetch_fn(**params) -> list[dict]
Source = tuple[str, Callable[..., list[dict]]]


@dataclass
class SourceFetch:
    """单个数据源的采集结果(归一化记录 + 错误信息)."""

    source: str
    records: list[dict] = field(default_factory=list)
    error: Optional[str] = None
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.records)


def collect_sources(
    sources: Iterable[Source], **params: Any,
) -> list[SourceFetch]:
    """逐源采集, 异常隔离: 任一源抛错只记入其 ``error``, 不打断后续源."""
    results: list[SourceFetch] = []
    for name, fn in sources:
        t0 = time.time()
        try:
            records = list(fn(**params) or [])
            results.append(
                SourceFetch(name, records, None, round(time.time() - t0, 2)),
            )
        except Exception as e:
            logger.warning("source '%s' fetch failed: %s", name, e)
            results.append(
                SourceFetch(
                    name, [], f"{type(e).__name__}: {e}",
                    round(time.time() - t0, 2),
                ),
            )
    return results


def merge_records(
    fetches: Iterable[SourceFetch],
    key: Callable[[dict], Any],
    limit: Optional[int] = None,
) -> tuple[list[dict], list[str]]:
    """按业务键合并去重; 同键记录保留先出现者(源列表顺序即优先级).

    返回 ``(合并后的记录列表, 实际贡献了记录的源名称列表)``;
    每条记录附加 ``_source`` 字段标明来源, 便于输出标注与排查.
    """
    merged: dict[Any, dict] = {}
    order: list[Any] = []
    used: list[str] = []
    for f in fetches:
        if not f.ok:
            continue
        contributed = False
        for rec in f.records:
            k = key(rec)
            if k in merged:
                continue
            merged[k] = {**rec, "_source": f.source}
            order.append(k)
            contributed = True
        if contributed:
            used.append(f.source)
    rows = [merged[k] for k in order]
    return (rows[:limit] if limit else rows), used


def first_success(
    sources: Iterable[Source], **params: Any,
) -> Optional[SourceFetch]:
    """主机级容灾: 按顺序尝试, 返回第一个成功且非空的采集结果."""
    for name, fn in sources:
        t0 = time.time()
        try:
            records = list(fn(**params) or [])
        except Exception as e:
            logger.warning("source '%s' fetch failed: %s", name, e)
            continue
        if records:
            return SourceFetch(name, records, None, round(time.time() - t0, 2))
    return None


def describe(fetches: Iterable[SourceFetch]) -> str:
    """采集结果概要(用于输出头部标注): '源A(12条) + 源B(3条)'."""
    parts = [
        f"{f.source}({len(f.records)}条)" if f.ok else f"{f.source}(失败)"
        for f in fetches
    ]
    return " + ".join(parts) if parts else "无可用数据源"
