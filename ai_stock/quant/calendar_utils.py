"""A-share trading calendar utilities.

交易日历: 优先使用 ``chinese_calendar``（本地缓存交易日，覆盖法定节假
日与调休）；未安装时降级为「周一至周五即交易日」的近似——对模拟盘
决策影响可控（节假日误判只会多一次空扫描，调度侧还有交易时段守卫）。

flow_state、5 交易日时间止损全部使用交易日计数，不用自然日。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

try:  # 优雅降级: pip install 'chinese-calendar'
    from chinese_calendar import is_workday, is_holiday
    _HAS_CN_CAL = True
except ImportError:
    _HAS_CN_CAL = False
    logger.info(
        "chinese_calendar 未安装, 交易日历降级为「工作日近似」; "
        "建议 pip install 'chinese-calendar'"
    )

# 内存缓存: date -> bool (进程内一次计算)
_cache: dict[date, bool] = {}


def is_trading_day(d: date | datetime | None = None) -> bool:
    """判断某日是否 A 股交易日 (节假日表优先, 降级为工作日近似)."""
    if d is None:
        d = datetime.now()
    if isinstance(d, datetime):
        d = d.date()
    if d in _cache:
        return _cache[d]

    if _HAS_CN_CAL:
        try:
            ok = is_workday(d) and not is_holiday(d)
        except NotImplementedError:
            # chinese_calendar 数据只覆盖到已发布的年份, 超出范围回退工作日近似
            ok = d.weekday() < 5
    else:
        ok = d.weekday() < 5

    _cache[d] = ok
    return ok


def get_trade_date(offset: int = 0, ref: date | None = None) -> str:
    """返回以 ref (默认今天) 为基准往前第 ``offset`` 个交易日的日期字符串.

    offset=0 → 最近一个已开始的交易日 (今天非交易日则回溯).
    """
    if ref is None:
        ref = date.today()
    cur = ref
    remaining = offset if offset > 0 else 0
    # 回溯到最近的交易日
    while not is_trading_day(cur):
        cur -= timedelta(days=1)
    while remaining > 0:
        cur -= timedelta(days=1)
        if is_trading_day(cur):
            remaining -= 1
    return cur.strftime("%Y-%m-%d")


def next_trading_day(ref: date | None = None) -> date:
    """返回 ref 之后的下一个交易日 (不含 ref 当天)."""
    cur = (ref or date.today()) + timedelta(days=1)
    while not is_trading_day(cur):
        cur += timedelta(days=1)
    return cur


def count_trading_days(start: str | date, end: str | date) -> int:
    """统计 [start, end] 闭区间内的交易日数 (含两端)."""
    if isinstance(start, str):
        start = datetime.strptime(start, "%Y-%m-%d").date()
    if isinstance(end, str):
        end = datetime.strptime(end, "%Y-%m-%d").date()
    if end < start:
        return 0
    n, cur = 0, start
    while cur <= end:
        if is_trading_day(cur):
            n += 1
        cur += timedelta(days=1)
    return n


def trading_days_between(start: str | date, end: str | date) -> list[date]:
    """返回 [start, end] 闭区间内的交易日列表."""
    if isinstance(start, str):
        start = datetime.strptime(start, "%Y-%m-%d").date()
    if isinstance(end, str):
        end = datetime.strptime(end, "%Y-%m-%d").date()
    days, cur = [], start
    while cur <= end:
        if is_trading_day(cur):
            days.append(cur)
        cur += timedelta(days=1)
    return days


def is_trading_time(dt: datetime | None = None) -> bool:
    """判断当前是否处于 A 股交易时段 (9:30-11:30 / 13:00-15:00, 仅交易日)."""
    from .config import AFTERNOON_SESSION, MORNING_SESSION

    if dt is None:
        dt = datetime.now()
    if not is_trading_day(dt):
        return False
    minutes = dt.hour * 60 + dt.minute
    m_start = MORNING_SESSION[0] * 60 + MORNING_SESSION[1]
    m_end = MORNING_SESSION[2] * 60 + MORNING_SESSION[3]
    a_start = AFTERNOON_SESSION[0] * 60 + AFTERNOON_SESSION[1]
    a_end = AFTERNOON_SESSION[2] * 60 + AFTERNOON_SESSION[3]
    return m_start <= minutes <= m_end or a_start <= minutes <= a_end


def advance_trading_days(start: date, n: int) -> date:
    """从 start 起向后数 n 个交易日 (start 本身不计入), 返回第 n 个交易日."""
    cur, remaining = start, n
    while remaining > 0:
        cur += timedelta(days=1)
        if is_trading_day(cur):
            remaining -= 1
    return cur
