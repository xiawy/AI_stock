"""新闻方向 → 矩阵符号与催化窗口 (M5 的一部分, 纯函数).

口径纪律: 只用新闻榜给出的**主题级 direction + strength**, 不用标题命中.
标题命中只能证明"这条新闻提到了锂", 证明不了"锂矿看涨"; 用后者凑方向,
会让矩阵在三条证据链其实只有两条时显示"三方共振" —— 伪共振正是这一层
要消灭的东西, 不能自己制造.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from ..contracts import NewsImpact
from . import textmatch

# 方向符号的强度门槛: 净强度 ≥2 才算"有方向", 单条 strength=1 的弱多不改变结论
MIN_STRENGTH = 2

_BULLISH = {"bullish", "positive", "利多", "看多", "up"}
_BEARISH = {"bearish", "negative", "利空", "看空", "down"}

_HORIZON_TDAYS = {"1d": 1, "3d": 3, "1w": 5}


def direction_sign(direction: str) -> int:
    text = (direction or "").strip().lower()
    if text in _BULLISH:
        return 1
    if text in _BEARISH:
        return -1
    return 0


def group(
    impacts: Sequence[NewsImpact], names: Sequence[str],
) -> dict[str, list[NewsImpact]]:
    """把新闻主题按篮子归组. 一个主题只归一个篮子 (最匹配的那个)."""
    buckets: dict[str, list[NewsImpact]] = {n: [] for n in names}
    for item in impacts:
        target = textmatch.best(item.theme, list(names))
        if target:
            buckets[target].append(item)
    return buckets


def sign(items: Sequence[NewsImpact], min_strength: int = MIN_STRENGTH) -> int:
    """主题方向净额 → 符号. 无证据或力度不足返回 0 (不猜方向)."""
    net = sum(direction_sign(i.direction) * max(int(i.strength or 1), 1) for i in items)
    if net >= min_strength:
        return 1
    if net <= -min_strength:
        return -1
    return 0


MARKET_FACT = "market:price+flow"
"""行情与资金是两条独立数据链 (板块行情 / 个股主力净额), 合成一条事实源."""


def independent_facts(items: Sequence[NewsImpact], frozen_tags: str = "") -> set[str]:
    """独立事实集合, 供伪共振判定.

    真正的伪共振不是"新闻多报了几家", 而是**新闻只是把涨停归因又说了一遍**:
    同花顺的 reason_tags 本身就是编辑对着同一批新闻写的归因, 它在矩阵里已算
    板块面证据; 若新闻主题与归因同名, 再算一次新闻面证据就是重复计数.

    所以: 与归因同名的新闻不计入独立事实; 剩余事实 + 行情/资金链 = 独立源集合.
    """
    facts = {MARKET_FACT}
    for item in items:
        if frozen_tags and item.theme and item.theme in frozen_tags:
            continue
        facts.add(f"{item.theme}@{item.base_date}")
    return facts


def catalyst(items: Sequence[NewsImpact]) -> str:
    """催化窗口文本. 只写新闻榜确实给过的 horizon 与预期区间.

    无 horizon 就不写日期 —— 编一个"预计 3 日内"是最廉价也最有害的伪精确.
    """
    usable = [i for i in items if direction_sign(i.direction) != 0]
    if not usable:
        return ""
    days = [_HORIZON_TDAYS.get((i.horizon or "").strip().lower(), 0) for i in usable]
    days = [d for d in days if d]
    span = f"T+{max(days)} 内" if days else ""
    lo = max((i.exp_low for i in usable), default=0.0)
    hi = max((i.exp_high for i in usable), default=0.0)
    band = f"预期 {lo:+.1f}~{hi:+.1f}%" if hi else ""
    parts = [p for p in (span, band) if p]
    return " · ".join(parts)


def summary(items: Sequence[NewsImpact]) -> str:
    """一行新闻面描述 (矩阵 news_view 用), 无证据时明确写"无". """
    usable = [i for i in items if direction_sign(i.direction) != 0]
    if not usable:
        return "无相关新闻方向"
    net = sum(direction_sign(i.direction) * max(int(i.strength or 1), 1) for i in usable)
    side = "偏正" if net > 0 else ("偏负" if net < 0 else "分歧")
    return f"{len(usable)} 条主题证据, 净强度 {net:+d} ({side})"


def coverage(buckets: Mapping[str, list[NewsImpact]]) -> int:
    """命中篮子的新闻条数, 用于报告里说明"新闻面参与度".

    0 条时报告必须显示"新闻面未参与", 而不是让读者以为矩阵已经三方对齐.
    """
    return sum(len(items) for items in buckets.values())
