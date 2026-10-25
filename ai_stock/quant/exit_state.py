"""持仓退出意图 (``pending_exit``) —— 卖出链路的唯一读写入口.

为什么需要它: 卖单被风控拒绝或跌停未成交后, 原实现只写一条日志就结束, 下一轮
扫描 (10 分钟) 走的是**完整决策链**, 要重新跑一遍 LLM 利空判断与趋势判断 ——
极端行情下连续拒单等于止损完全失效。退出意图把「已经决定要卖、但还没卖掉」
这个事实持久化下来, 让下一轮扫描跳过决策直接重试, 直到成交。

存储位置: ``quant_stock_pool_holding.plan_json`` 的 ``pending_exit`` 子键。
量化表没有 Alembic 迁移链路 (``create_all`` 不会给已存在的表补列), 所以新增
状态一律落在 JSON 字段里; 顺带随持仓生命周期自动清理, 无需归档逻辑。

两种意图, 靠 ``confirm_needed`` 区分, 调用方不要用别的方式猜:

- ``confirm_needed > 0``: 决策还没确认 (重大利空首次判定), **不强制卖出** ——
  下一轮照常走完整流程重新判定;
- ``confirm_needed == 0``: 卖出已被决策过但没成交, **必须跳过决策直接重试**
  (``is_actionable``).

**所有读写必须走本模块**, 且只能通过 ``db_ops.mutate_holding_plan`` 原子完成:
``session_scope`` 每次是新 session, 先读后写会让并发的 ``_update_plan``
互相覆盖整份 plan。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from . import db_ops
from .calendar_utils import market_now
from .config import SELL_RETRY_COOLDOWN_MINUTES, SELL_RETRY_MAX_PER_DAY

logger = logging.getLogger(__name__)

# plan_json 中的子键名
PENDING_KEY = "pending_exit"


def enabled() -> bool:
    """退出意图重试开关 (运行时键, 改库即生效; 0 = 回到「只记日志」的旧行为)."""
    return db_ops.get_flag("sell_retry_enabled", True)


def get_pending_exit(holding: Optional[dict]) -> Optional[dict]:
    """读退出意图; 持仓 dict 直接来自 ``db_ops.get_holding(s)``."""
    if not holding:
        return None
    return (holding.get("plan") or {}).get(PENDING_KEY) or None


def is_actionable(pending: Optional[dict]) -> bool:
    """True = 已有卖出决策但没成交, 必须跳过决策直接重试.

    重大利空「待确认」状态不算 —— 那种情况下需要重新判定, 而不是照旧卖出。
    """
    if not pending:
        return False
    return int(pending.get("confirm_needed") or 0) == 0


def mark_pending_exit(
    symbol: str,
    *,
    reason: str = "",
    portion: float = 1.0,
    error: str = "",
    confirm_needed: int = 0,
) -> Optional[dict]:
    """写入/累加退出意图, 返回最新状态.

    幂等: 重复调用只累加计数, ``first_at`` 保留首次时间, ``portion`` 以首次
    为准 —— 同一笔退出反复重试时必须卖同样的量, 不能因为重试次数变化而漂移。
    ``error`` 非空表示本次是「尝试卖出但失败」, 计入 ``fail_count``。
    """
    now = market_now()
    today = now.strftime("%Y-%m-%d")

    def _mutate(plan: dict) -> None:
        cur = dict(plan.get(PENDING_KEY) or {})
        same_day = cur.get("day") == today
        plan[PENDING_KEY] = {
            "reason": reason or cur.get("reason", ""),
            "portion": float(cur.get("portion", portion)),
            "first_at": cur.get("first_at") or now.isoformat(timespec="seconds"),
            "last_attempt_at": now.isoformat(timespec="seconds"),
            "attempts": int(cur.get("attempts") or 0) + 1,
            "day": today,
            "day_attempts": (int(cur.get("day_attempts") or 0) if same_day else 0) + 1,
            "fail_count": int(cur.get("fail_count") or 0) + (1 if error else 0),
            "last_error": error or "",
            "confirm_needed": int(confirm_needed),
        }

    plan = db_ops.mutate_holding_plan(symbol, _mutate)
    if plan is None:
        # 持仓已不存在 (清仓完成/人工删除), 无需记录
        return None
    return plan.get(PENDING_KEY)


def clear_pending_exit(symbol: str) -> None:
    """成交后清除退出意图 (卖出已落地, 意图没有意义了)."""
    db_ops.mutate_holding_plan(symbol, lambda plan: plan.pop(PENDING_KEY, None))


def mark_and_notify(
    symbol: str,
    *,
    reason: str = "",
    portion: float = 1.0,
    error: str = "",
    confirm_needed: int = 0,
    queued: bool = False,
) -> Optional[dict]:
    """记录退出意图并按失败次数分级告警 —— 卖出链路的统一入口.

    ``queued=True`` 表示跌停挂单排队 (非拒单), 文案区分开便于运维判断。
    告警类型必须带上标的 (``sell_blocked:<symbol>``): ``emit`` 对同一
    ``alert_type`` 有静默窗口, 用固定类型会让第一只票的告警把其余票全部静默掉。
    """
    pending = mark_pending_exit(
        symbol, reason=reason, portion=portion, error=error, confirm_needed=confirm_needed,
    )
    if pending is None:
        return None
    from .ops_monitor import ERROR, WARNING, get_alerts

    fail_count = int(pending.get("fail_count") or 0)
    level = ERROR if fail_count >= 3 else WARNING
    action = "挂单未成交" if queued else "被拒"
    get_alerts().emit(
        level, f"sell_blocked:{symbol}",
        f"{symbol} 卖出{action} ({error or '未知原因'}), 累计失败 {fail_count} 次, "
        f"将按 {SELL_RETRY_COOLDOWN_MINUTES} 分钟冷却重试",
    )
    return pending


def should_retry(pending: Optional[dict], now: Optional[datetime] = None) -> bool:
    """当前是否到了可以再试一次的时刻 (冷却 + 当日次数上限).

    冷却的作用不是省资源, 而是避免每 10 分钟的扫描窗口都起一个 flow、重复打
    ``pre_trade_check`` 与告警; 次数上限保证「一直卖不掉」时当日收敛并告警,
    不再无限重试。
    """
    if not pending:
        return False
    now = now or market_now()
    today = now.strftime("%Y-%m-%d")
    if pending.get("day") == today:
        if int(pending.get("day_attempts") or 0) >= SELL_RETRY_MAX_PER_DAY:
            return False
    last = _parse_iso(pending.get("last_attempt_at"))
    if last and now - last < timedelta(minutes=SELL_RETRY_COOLDOWN_MINUTES):
        return False
    return True


def _parse_iso(raw: Optional[str]) -> Optional[datetime]:
    """解析写入的 ISO 时间; 无时区按市场时区解释 (旧数据兼容)."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=market_now().tzinfo)
