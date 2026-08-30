"""CRUD operations for the quant subsystem tables.

所有函数短事务 + 异常兜底（失败记日志、返回安全默认值），Agent 消费线程
可直接调用而不必关心 session 生命周期。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import delete, update

from .db import session_scope
from .db_models import (
    AgentDecisionLog,
    ConsumerHeartbeat,
    DataSourceCircuit,
    EvolutionHistory,
    NewsVectorMeta,
    QuantCacheData,
    QuantFlowState,
    QuantIndustryBoard,
    QuantTask,
    RuleTestCase,
    StockPoolHolding,
    StockPoolOptional,
    StrategyRule,
    SystemConfig,
    TradeLog,
    UserAccount,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# A 股市场时区: 交易日的日界必须按北京时间切分 (UTC 日界会把北京凌晨~上午 8 点前的委托错归前一天).
# 注意: SQLite 的 DateTime 比较是字符串字典序, 存储侧统一 +00:00 后缀,
# 查询边界必须先换算成 UTC 再传入, 混合偏移后缀会导致过滤失效.
_MARKET_TZ = timezone(timedelta(hours=8))


def _market_day_start(date_str: Optional[str] = None) -> datetime:
    """返回北京日历日零点 (已换算为 UTC, 供 trade_time 查询边界用)."""
    if date_str:
        local = datetime.strptime(date_str, "%Y-%m-%d")
    else:
        local = datetime.now(_MARKET_TZ).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
    return local.replace(tzinfo=_MARKET_TZ).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# system_config
# ---------------------------------------------------------------------------

def get_config_value(key: str, default: str = "") -> str:
    with session_scope() as s:
        row = s.get(SystemConfig, key)
        return row.value if row is not None else default


def set_config_value(key: str, value: str, description: str = "") -> None:
    with session_scope() as s:
        row = s.get(SystemConfig, key)
        if row is None:
            s.add(SystemConfig(key=key, value=value, description=description))
        else:
            row.value = value
            if description:
                row.description = description
        s.commit()


def get_all_config() -> dict[str, str]:
    with session_scope() as s:
        rows = s.query(SystemConfig).all()
        return {r.key: r.value for r in rows}


# ---------------------------------------------------------------------------
# stock_pool_optional (自选池)
# ---------------------------------------------------------------------------

def upsert_optional_stock(item: dict) -> bool:
    """新增/更新自选池标的 (status=active, observe_expire 按交易日计)."""
    from .calendar_utils import advance_trading_days
    from .config import OBSERVE_EXPIRE_TRADING_DAYS

    symbol = item.get("symbol", "")
    if not symbol:
        return False
    with session_scope() as s:
        row = s.get(StockPoolOptional, symbol)
        if row is None:
            row = StockPoolOptional(symbol=symbol)
            s.add(row)
        row.name = item.get("name", "")
        row.industry = item.get("industry", "")
        row.reason = item.get("reason", "")
        row.bull_factors = json.dumps(item.get("bull_factors", []), ensure_ascii=False)
        row.bear_factors = json.dumps(item.get("bear_factors", []), ensure_ascii=False)
        row.stage_judgement = item.get("stage_judgement", "")
        row.rise_trigger = item.get("rise_trigger", "")
        row.report = item.get("report", "")
        row.risk_tags_json = json.dumps(item.get("risk_tags", []), ensure_ascii=False)
        row.confidence = float(item.get("confidence", 0.0))
        row.status = "active"
        row.add_time = _now()
        row.remove_reason = ""
        row.remove_time = None
        row.observe_expire = advance_trading_days(
            datetime.now().date(), OBSERVE_EXPIRE_TRADING_DAYS,
        )
        s.commit()
    return True


def get_optional_pool(status: str = "active") -> list[dict]:
    with session_scope() as s:
        rows = (
            s.query(StockPoolOptional)
            .filter(StockPoolOptional.status == status)
            .all()
        )
        return [r.to_dict() for r in rows]


def get_optional_stock(symbol: str) -> Optional[dict]:
    with session_scope() as s:
        row = s.get(StockPoolOptional, symbol)
        return row.to_dict() if row else None


def get_optional_pool_as_of(date_str: str) -> list[dict]:
    """指定日期 (YYYY-MM-DD) 当日处于观察中的自选池快照 (热股榜历史视图).

    判定: add_time <= 当日末 且 (未移出 或 remove_time >= 当日起点)。
    存储的 UTC 时间按日期边界换算本地日。
    """
    try:
        day_start = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return []
    # 本地日 → UTC 边界 (与写入侧 _now() 的 UTC 口径对齐, 取保守宽窗口)
    start_utc = day_start.replace(hour=0, minute=0, tzinfo=timezone.utc) - timedelta(hours=14)
    end_utc = start_utc + timedelta(days=1)
    with session_scope() as s:
        rows = (
            s.query(StockPoolOptional)
            .filter(StockPoolOptional.add_time <= end_utc)
            .all()
        )
        out = []
        for r in rows:
            removed = r.remove_time is not None and (
                r.remove_time.replace(tzinfo=timezone.utc)
                if r.remove_time.tzinfo is None else r.remove_time
            ) < start_utc
            if not removed:
                out.append(r.to_dict())
        return out


def update_optional_status(
    symbol: str,
    status: str,
    remove_reason: str = "",
) -> bool:
    """更新自选池状态 (removed / expired / bought / active)."""
    with session_scope() as s:
        row = s.get(StockPoolOptional, symbol)
        if row is None:
            return False
        row.status = status
        if status in ("removed", "expired"):
            row.remove_reason = remove_reason
            row.remove_time = _now()
        s.commit()
        return True


def expire_stale_optional(today: datetime | None = None) -> int:
    """观察期到期 → status=expired (每日报告前跑一次).

    observe_expire 存的是交易日 date: 必须按"日期"比较 (过期日当天仍可观察,
    次日起才到期), 用 aware datetime 直接比会在过期日零点就提前出池, 观察期少一天.
    """
    now = today or _now()
    expire_before = now.date() if isinstance(now, datetime) else now
    with session_scope() as s:
        result = s.execute(
            update(StockPoolOptional)
            .where(
                StockPoolOptional.status == "active",
                StockPoolOptional.observe_expire < expire_before,
            )
            .values(status="expired", remove_reason="观察期到期", remove_time=now)
        )
        s.commit()
        return result.rowcount or 0


# ---------------------------------------------------------------------------
# quant_industry_board (行业榜: 选股流程产出)
# ---------------------------------------------------------------------------

def save_industry_board(rank_date: str, rows: list[dict]) -> int:
    """覆盖写入指定日期的行业榜 (先删当日旧数据再插入)."""
    if not rank_date or not rows:
        return 0
    try:
        with session_scope() as s:
            s.execute(
                delete(QuantIndustryBoard).where(
                    QuantIndustryBoard.rank_date == rank_date
                )
            )
            for row in rows:
                s.add(QuantIndustryBoard(
                    rank_date=rank_date,
                    rank=int(row.get("rank", 0)),
                    industry=row.get("industry", ""),
                    industry_code=row.get("industry_code", ""),
                    industry_level=row.get("industry_level", ""),
                    stage=row.get("stage", ""),
                    event_tag=row.get("event_tag", ""),
                    heat_score=float(row.get("heat_score", 0.0) or 0.0),
                    change_pct=row.get("change_pct"),
                    main_net_inflow=row.get("main_net_inflow"),
                    leader_stocks_json=json.dumps(
                        row.get("leader_stocks", []), ensure_ascii=False,
                    ),
                ))
            s.commit()
        return len(rows)
    except Exception as exc:
        logger.error("Failed to save industry board for %s: %s", rank_date, exc)
        return 0


def get_latest_industry_board() -> Optional[dict]:
    """最新一期行业榜 (最近一个有数据的日期)."""
    with session_scope() as s:
        latest_date = (
            s.query(QuantIndustryBoard.rank_date)
            .order_by(QuantIndustryBoard.rank_date.desc())
            .limit(1)
            .scalar()
        )
    if not latest_date:
        return None
    return get_industry_board_by_date(latest_date)


def get_industry_board_by_date(date_str: str) -> Optional[dict]:
    """按日期查行业榜; 无数据返回 None."""
    with session_scope() as s:
        rows = (
            s.query(QuantIndustryBoard)
            .filter(QuantIndustryBoard.rank_date == date_str)
            .order_by(QuantIndustryBoard.rank.asc())
            .all()
        )
        if not rows:
            return None
        return {
            "rank_date": date_str,
            "created_at": (
                rows[0].created_at.isoformat() if rows[0].created_at else None
            ),
            "rankings": [r.to_dict() for r in rows],
        }


def get_industry_board_row(row_id: int) -> Optional[dict]:
    with session_scope() as s:
        row = s.get(QuantIndustryBoard, row_id)
        return row.to_dict() if row else None


def cleanup_industry_board(days: int = 70) -> int:
    """保留窗口清理: 删除 N 天前的行业榜数据."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with session_scope() as s:
        result = s.execute(
            delete(QuantIndustryBoard).where(
                QuantIndustryBoard.rank_date < cutoff
            )
        )
        s.commit()
        return result.rowcount or 0


# ---------------------------------------------------------------------------
# stock_pool_holding (持仓池)
# ---------------------------------------------------------------------------

def upsert_holding(item: dict, user_id: str | None = None) -> bool:
    from .config import SYSTEM_USER

    uid = user_id or item.get("user_id") or SYSTEM_USER
    symbol = item.get("symbol", "")
    if not symbol:
        return False
    with session_scope() as s:
        row = s.get(StockPoolHolding, (uid, symbol))
        if row is None:
            row = StockPoolHolding(user_id=uid, symbol=symbol)
            s.add(row)
        row.name = item.get("name", row.name or "")
        row.quantity = int(item.get("quantity", row.quantity or 0))
        row.available_quantity = int(
            item.get("available_quantity", row.available_quantity or 0)
        )
        row.cost_price = float(item.get("cost_price", row.cost_price or 0.0))
        row.entry_reason = item.get("entry_reason", row.entry_reason or "")
        row.plan_json = json.dumps(item.get("plan", {}), ensure_ascii=False) \
            if "plan" in item else row.plan_json
        row.predicted_path_json = json.dumps(
            item.get("predicted_path", {}), ensure_ascii=False,
        ) if "predicted_path" in item else row.predicted_path_json
        row.stop_loss = float(item.get("stop_loss", row.stop_loss or 0.0))
        row.take_profit = float(item.get("take_profit", row.take_profit or 0.0))
        row.last_review_time = _now()
        if "cannot_sell_until" in item:
            row.cannot_sell_until = item["cannot_sell_until"]
        s.commit()
    return True


def get_holdings(user_id: str | None = None) -> list[dict]:
    """user_id 缺省为系统账户 (保持既有引擎链路口径); '__all__' 返回全部用户."""
    from .config import SYSTEM_USER

    with session_scope() as s:
        q = s.query(StockPoolHolding)
        uid = SYSTEM_USER if user_id is None else user_id
        if uid != "__all__":
            q = q.filter(StockPoolHolding.user_id == uid)
        rows = q.all()
        return [r.to_dict() for r in rows]


def get_holding(symbol: str, user_id: str | None = None) -> Optional[dict]:
    from .config import SYSTEM_USER

    with session_scope() as s:
        row = s.get(StockPoolHolding, (user_id or SYSTEM_USER, symbol))
        return row.to_dict() if row else None


def delete_holding(symbol: str, user_id: str | None = None) -> bool:
    from .config import SYSTEM_USER

    with session_scope() as s:
        row = s.get(StockPoolHolding, (user_id or SYSTEM_USER, symbol))
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def update_holding_plan(symbol: str, plan: dict, user_id: str | None = None) -> bool:
    from .config import SYSTEM_USER

    with session_scope() as s:
        row = s.get(StockPoolHolding, (user_id or SYSTEM_USER, symbol))
        if row is None:
            return False
        row.plan_json = json.dumps(plan, ensure_ascii=False)
        row.last_review_time = _now()
        s.commit()
        return True


# ---------------------------------------------------------------------------
# trade_log
# ---------------------------------------------------------------------------

def add_trade_log(entry: dict) -> Optional[int]:
    from .config import SYSTEM_USER

    with session_scope() as s:
        row = TradeLog(
            user_id=entry.get("user_id", SYSTEM_USER),
            order_id=entry.get("order_id", ""),
            symbol=entry.get("symbol", ""),
            name=entry.get("name", ""),
            side=entry.get("side", ""),
            price=float(entry.get("price", 0.0)),
            quantity=int(entry.get("quantity", 0)),
            amount=float(entry.get("amount", 0.0)),
            fee=float(entry.get("fee", 0.0)),
            status=entry.get("status", "filled"),
            reason=entry.get("reason", ""),
            broker=entry.get("broker", "simulated"),
            cost_price=float(entry.get("cost_price", 0.0)),
            realized_pnl=float(entry.get("realized_pnl", 0.0)),
            pnl_pct=float(entry.get("pnl_pct", 0.0)),
            trade_time=_now(),
        )
        s.add(row)
        s.commit()
        return row.id


def get_trades(
    date_str: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = 200,
    user_id: str | None = None,
) -> list[dict]:
    """user_id 缺省为系统账户 (保持既有引擎链路口径); '__all__' 返回全部用户."""
    from .config import SYSTEM_USER

    uid = SYSTEM_USER if user_id is None else user_id
    with session_scope() as s:
        q = s.query(TradeLog).order_by(TradeLog.trade_time.desc())
        if uid != "__all__":
            q = q.filter(TradeLog.user_id == uid)
        if symbol:
            q = q.filter(TradeLog.symbol == symbol)
        if date_str:
            day = _market_day_start(date_str)
            q = q.filter(
                TradeLog.trade_time >= day,
                TradeLog.trade_time < day + timedelta(days=1),
            )
        rows = q.limit(limit).all()
        return [r.to_dict() for r in rows]


def count_orders_today(date_str: Optional[str] = None) -> int:
    """当日委托笔数 (含 rejected, 用于 max_daily_orders 校验; 按北京时间日界)."""
    day = _market_day_start(date_str)
    with session_scope() as s:
        return (
            s.query(TradeLog)
            .filter(
                TradeLog.trade_time >= day,
                TradeLog.trade_time < day + timedelta(days=1),
            )
            .count()
        )


# ---------------------------------------------------------------------------
# user_account (多用户模拟账户)
# ---------------------------------------------------------------------------

def get_user_account(user_id: str) -> Optional[dict]:
    with session_scope() as s:
        row = s.get(UserAccount, user_id)
        return row.to_dict() if row else None


def get_user_accounts(active_only: bool = False) -> list[dict]:
    with session_scope() as s:
        q = s.query(UserAccount).order_by(UserAccount.created_at.asc())
        if active_only:
            q = q.filter(UserAccount.status == "active")
        return [r.to_dict() for r in q.all()]


def upsert_user_account(item: dict) -> bool:
    """创建用户账户; 已存在时仅同步 username/状态, 不触碰资金 (幂等开户)."""
    user_id = str(item.get("user_id", "")).strip()
    if not user_id:
        return False
    with session_scope() as s:
        row = s.get(UserAccount, user_id)
        if row is None:
            capital = float(item.get("initial_capital", 0.0))
            row = UserAccount(
                user_id=user_id,
                username=item.get("username", ""),
                initial_capital=capital,
                cash_balance=float(item.get("cash_balance", capital)),
                status=item.get("status", "active"),
            )
            s.add(row)
        else:
            if item.get("username"):
                row.username = item["username"]
            if item.get("status"):
                row.status = item["status"]
        s.commit()
    return True


def set_user_account_status(user_id: str, status: str) -> bool:
    """更新账户状态 (active / frozen); 账户不存在返回 False."""
    with session_scope() as s:
        row = s.get(UserAccount, user_id)
        if row is None:
            return False
        row.status = status
        s.commit()
        return True


def adjust_user_cash(user_id: str, delta: float) -> Optional[float]:
    """调整用户可用资金 (买入为负/卖出为正), 返回变动后余额; 无账户返回 None."""
    with session_scope() as s:
        row = s.get(UserAccount, user_id)
        if row is None:
            return None
        row.cash_balance = round((row.cash_balance or 0.0) + float(delta), 2)
        s.commit()
        return row.cash_balance


def get_user_trade_stats(user_id: str) -> dict:
    """用户交易统计: 以已成交卖出为一笔闭环交易, 统计胜率与累计盈亏."""
    trades = get_trades(limit=5000, user_id=user_id)
    closed = [
        t for t in trades
        if t["side"] == "sell" and t["status"] == "filled"
    ]
    wins = [t for t in closed if (t.get("realized_pnl") or 0.0) > 0]
    losses = [t for t in closed if (t.get("realized_pnl") or 0.0) <= 0]
    total_pnl = round(sum(t.get("realized_pnl") or 0.0 for t in closed), 2)
    avg_pct = (
        round(sum(t.get("pnl_pct") or 0.0 for t in closed) / len(closed), 4)
        if closed else 0.0
    )
    return {
        "user_id": user_id,
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed), 4) if closed else 0.0,
        "total_realized_pnl": total_pnl,
        "avg_pnl_pct": avg_pct,
    }


def get_realized_pnl_today(date_str: Optional[str] = None, user_id: str | None = None) -> float:
    """当日已实现盈亏.

    优先汇总卖出流水中已记录的 realized_pnl (broker/用户卖出均写入);
    旧数据无记录时降级为当日买卖配对的近似口径兜底.
    """
    trades = get_trades(date_str=date_str, limit=1000, user_id=user_id)
    sells = [t for t in trades if t["side"] == "sell" and t["status"] == "filled"]
    recorded = [t for t in sells if (t.get("cost_price") or 0.0) > 0]
    if recorded:
        return round(sum(t.get("realized_pnl") or 0.0 for t in recorded), 2)
    # 兜底: 旧数据近似口径 (当日卖出回款 - 卖出份额×当日买入均价)
    buy_amount, sell_net = 0.0, 0.0
    buy_qty, sell_qty = 0, 0
    for t in trades:
        if t["side"] == "buy" and t["status"] == "filled":
            buy_amount += t["amount"] + t["fee"]
            buy_qty += t["quantity"]
        elif t["side"] == "sell" and t["status"] == "filled":
            sell_net += t["amount"] - t["fee"]
            sell_qty += t["quantity"]
    if sell_qty == 0 or buy_qty == 0:
        return 0.0
    avg_cost = buy_amount / buy_qty
    return sell_net - sell_qty * avg_cost


# ---------------------------------------------------------------------------
# agent_decision_log
# ---------------------------------------------------------------------------

def log_decision(
    agent: str,
    decision: str,
    reason: str = "",
    symbol: str = "",
    task_id: str = "",
    flow_id: str = "",
    detail: dict | None = None,
) -> None:
    """写决策日志 (Agent 审计主通道, 永不抛异常)."""
    try:
        with session_scope() as s:
            s.add(AgentDecisionLog(
                agent=agent,
                task_id=task_id,
                flow_id=flow_id,
                symbol=symbol,
                decision=decision,
                reason=reason[:4000],
                detail_json=json.dumps(detail or {}, ensure_ascii=False, default=str),
            ))
            s.commit()
    except Exception as exc:
        logger.error("log_decision failed (%s/%s): %s", agent, decision, exc)


def get_decisions(
    limit: int = 100,
    agent: Optional[str] = None,
    symbol: Optional[str] = None,
    flow_id: Optional[str] = None,
) -> list[dict]:
    with session_scope() as s:
        q = s.query(AgentDecisionLog).order_by(AgentDecisionLog.ts.desc())
        if agent:
            q = q.filter(AgentDecisionLog.agent == agent)
        if symbol:
            q = q.filter(AgentDecisionLog.symbol == symbol)
        if flow_id:
            q = q.filter(AgentDecisionLog.flow_id == flow_id)
        return [r.to_dict() for r in q.limit(limit).all()]


# ---------------------------------------------------------------------------
# flow_state (Orchestrator)
# ---------------------------------------------------------------------------

def create_flow(
    flow_id: str,
    flow_type: str,
    timeout_seconds: int,
    allow_fallback: bool = False,
    data: dict | None = None,
) -> Optional[dict]:
    with session_scope() as s:
        if s.get(QuantFlowState, flow_id) is not None:
            return None  # 幂等: flow_id 已存在
        row = QuantFlowState(
            flow_id=flow_id,
            flow_type=flow_type,
            status="running",
            current_step="",
            completed_steps_json="[]",
            data_json=json.dumps(data or {}, ensure_ascii=False, default=str),
            timeout_seconds=timeout_seconds,
            allow_fallback=allow_fallback,
        )
        s.add(row)
        s.commit()
        return row.to_dict()


def get_flow(flow_id: str) -> Optional[dict]:
    with session_scope() as s:
        row = s.get(QuantFlowState, flow_id)
        return row.to_dict() if row else None


def update_flow(
    flow_id: str,
    status: Optional[str] = None,
    current_step: Optional[str] = None,
    completed_steps: Optional[list] = None,
    data: Optional[dict] = None,
    error: Optional[str] = None,
) -> bool:
    """更新 flow 状态; data 与既有 data 深合并 (每步关键结果累积)."""
    with session_scope() as s:
        row = s.get(QuantFlowState, flow_id)
        if row is None:
            return False
        if status is not None:
            row.status = status
        if current_step is not None:
            row.current_step = current_step
        if completed_steps is not None:
            row.completed_steps_json = json.dumps(completed_steps)
        if data is not None:
            merged = json.loads(row.data_json or "{}")
            merged.update(data)
            row.data_json = json.dumps(merged, ensure_ascii=False, default=str)
        if error is not None:
            row.error = error[:4000]
        row.updated_at = _now()
        s.commit()
        return True


def get_running_flows(flow_type: Optional[str] = None) -> list[dict]:
    with session_scope() as s:
        q = s.query(QuantFlowState).filter(QuantFlowState.status == "running")
        if flow_type:
            q = q.filter(QuantFlowState.flow_type == flow_type)
        return [r.to_dict() for r in q.all()]


def get_last_completed_flow(flow_type: str) -> Optional[dict]:
    with session_scope() as s:
        row = (
            s.query(QuantFlowState)
            .filter(
                QuantFlowState.flow_type == flow_type,
                QuantFlowState.status == "completed",
            )
            .order_by(QuantFlowState.updated_at.desc())
            .first()
        )
        return row.to_dict() if row else None


# ---------------------------------------------------------------------------
# consumer_heartbeat
# ---------------------------------------------------------------------------

def beat(consumer_id: str, queue_name: str, meta: dict | None = None) -> None:
    try:
        with session_scope() as s:
            row = (
                s.query(ConsumerHeartbeat)
                .filter(ConsumerHeartbeat.consumer_id == consumer_id)
                .first()
            )
            if row is None:
                s.add(ConsumerHeartbeat(
                    consumer_id=consumer_id,
                    queue_name=queue_name,
                    metadata_json=json.dumps(meta or {}),
                ))
            else:
                row.last_beat = _now()
                row.queue_name = queue_name
                row.metadata_json = json.dumps(meta or {})
            s.commit()
    except Exception as exc:
        logger.debug("heartbeat write failed for %s: %s", consumer_id, exc)


def get_stale_consumers(older_than_seconds: int = 120) -> list[dict]:
    """心跳超时的消费者 (进程死亡判定, 重启恢复用)."""
    cutoff = _now() - timedelta(seconds=older_than_seconds)
    with session_scope() as s:
        rows = s.query(ConsumerHeartbeat).filter(
            ConsumerHeartbeat.last_beat < cutoff,
        ).all()
        return [
            {
                "consumer_id": r.consumer_id,
                "queue_name": r.queue_name,
                "last_beat": r.last_beat.isoformat() if r.last_beat else None,
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# strategy rules / test cases / evolution
# ---------------------------------------------------------------------------

def upsert_rule(rule: dict) -> bool:
    rule_id = rule.get("rule_id", "")
    if not rule_id:
        return False
    with session_scope() as s:
        row = s.get(StrategyRule, rule_id)
        if row is None:
            row = StrategyRule(rule_id=rule_id)
            s.add(row)
        row.description = rule.get("description", "")
        row.condition = rule.get("condition", "")
        row.action = rule.get("action", "")
        row.priority = int(rule.get("priority", 5))
        row.enabled = bool(rule.get("enabled", True))
        row.version = int(rule.get("version", 1))
        row.test_case_ids_json = json.dumps(rule.get("test_case_ids", []))
        row.gray_scale = bool(rule.get("gray_scale", False))
        row.min_sample_out_perf = float(rule.get("min_sample_out_perf", 0.0))
        s.commit()
    return True


def get_rules(enabled_only: bool = False) -> list[dict]:
    with session_scope() as s:
        q = s.query(StrategyRule).order_by(StrategyRule.priority.asc())
        if enabled_only:
            q = q.filter(StrategyRule.enabled.is_(True))
        return [r.to_dict() for r in q.all()]


def add_test_case(rule_id: str, case_name: str, input_ctx: dict, expected: bool) -> int:
    with session_scope() as s:
        row = RuleTestCase(
            rule_id=rule_id,
            case_name=case_name,
            input_json=json.dumps(input_ctx, ensure_ascii=False),
            expected=expected,
        )
        s.add(row)
        s.commit()
        return row.id


def get_test_cases(rule_id: str) -> list[dict]:
    with session_scope() as s:
        rows = (
            s.query(RuleTestCase)
            .filter(RuleTestCase.rule_id == rule_id, RuleTestCase.enabled.is_(True))
            .all()
        )
        return [r.to_dict() for r in rows]


def add_evolution_record(entry: dict) -> int:
    with session_scope() as s:
        row = EvolutionHistory(
            rule_id=entry.get("rule_id", ""),
            params_json=json.dumps(entry.get("params", {}), ensure_ascii=False),
            train_metrics_json=json.dumps(entry.get("train_metrics", {})),
            valid_metrics_json=json.dumps(entry.get("valid_metrics", {})),
            oos_metrics_json=json.dumps(entry.get("oos_metrics", {})),
            status=entry.get("status", "draft"),
            reviewer_note=entry.get("reviewer_note", ""),
        )
        s.add(row)
        s.commit()
        return row.id


def update_evolution_status(evo_id: int, status: str, note: str = "") -> bool:
    with session_scope() as s:
        row = s.get(EvolutionHistory, evo_id)
        if row is None:
            return False
        row.status = status
        row.reviewer_note = note
        s.commit()
        return True


def get_evolution_records(status: Optional[str] = None, limit: int = 50) -> list[dict]:
    with session_scope() as s:
        q = s.query(EvolutionHistory).order_by(EvolutionHistory.ts.desc())
        if status:
            q = q.filter(EvolutionHistory.status == status)
        return [r.to_dict() for r in q.limit(limit).all()]


# ---------------------------------------------------------------------------
# cache_data (三级缓存冷归档)
# ---------------------------------------------------------------------------

def _aware(dt) -> datetime:
    """SQLite 读回的 naive datetime 统一补 UTC 时区, 避免与 aware 比较报错."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def cache_get(cache_key: str) -> Optional[dict]:
    with session_scope() as s:
        row = s.get(QuantCacheData, cache_key)
        if row is None:
            return None
        if row.expires_at is not None and _aware(row.expires_at) < _now():
            return None
        try:
            return json.loads(row.payload)
        except (TypeError, ValueError):
            return None


def cache_put(
    cache_key: str,
    value: Any,
    category: str = "",
    ttl_seconds: int = 3600,
) -> None:
    with session_scope() as s:
        row = s.get(QuantCacheData, cache_key)
        if row is None:
            row = QuantCacheData(cache_key=cache_key)
            s.add(row)
        row.category = category
        row.payload = json.dumps(value, ensure_ascii=False, default=str)
        row.expires_at = _now() + timedelta(seconds=ttl_seconds)
        row.created_at = _now()
        s.commit()


def cache_cleanup(keep_days: int = 3) -> int:
    cutoff = _now() - timedelta(days=keep_days)
    with session_scope() as s:
        rows = s.query(QuantCacheData).all()
        expired = [
            r.cache_key for r in rows
            if r.expires_at is not None and _aware(r.expires_at) < cutoff
        ]
        result = 0
        if expired:
            result = s.execute(
                delete(QuantCacheData).where(QuantCacheData.cache_key.in_(expired))
            ).rowcount or 0
        s.commit()
        return result


# ---------------------------------------------------------------------------
# news vector meta
# ---------------------------------------------------------------------------

def news_meta_exists(news_hash: str) -> bool:
    with session_scope() as s:
        return (
            s.query(NewsVectorMeta.id)
            .filter(NewsVectorMeta.news_hash == news_hash)
            .first()
            is not None
        )


def news_meta_add(item: dict) -> bool:
    """新增新闻元数据; 已存在返回 False (去重)."""
    h = item.get("news_hash", "")
    if not h:
        return False
    with session_scope() as s:
        if news_meta_exists(h):
            return False
        s.add(NewsVectorMeta(
            news_hash=h,
            title=item.get("title", "")[:512],
            content=item.get("content", ""),
            source=item.get("source", ""),
            pub_time=item.get("pub_time", ""),
            symbol=item.get("symbol", ""),
        ))
        s.commit()
        return True


def news_meta_search(
    keywords: list[str],
    days: int = 7,
    limit: int = 20,
) -> list[dict]:
    """SQLite 降级检索: 关键词 LIKE + 时间窗口 (无向量语义, 但保底可用)."""
    cutoff = _now() - timedelta(days=days)
    with session_scope() as s:
        q = s.query(NewsVectorMeta).filter(NewsVectorMeta.added_at >= cutoff)
        rows = q.order_by(NewsVectorMeta.added_at.desc()).limit(500).all()
        results = []
        for r in rows:
            text = f"{r.title} {r.content[:500]}"
            if any(kw in text for kw in keywords):
                results.append({
                    "news_hash": r.news_hash,
                    "title": r.title,
                    "content": r.content[:500],
                    "source": r.source,
                    "pub_time": r.pub_time,
                    "symbol": r.symbol,
                })
                if len(results) >= limit:
                    break
        return results


def news_meta_cleanup(days: int = 30) -> int:
    cutoff = _now() - timedelta(days=days)
    with session_scope() as s:
        result = s.execute(delete(NewsVectorMeta).where(NewsVectorMeta.added_at < cutoff))
        s.commit()
        return result.rowcount or 0


# ---------------------------------------------------------------------------
# data source circuit
# ---------------------------------------------------------------------------

def circuit_get(source_name: str) -> dict:
    with session_scope() as s:
        row = s.get(DataSourceCircuit, source_name)
        if row is None:
            return {"source_name": source_name, "state": "closed", "failure_count": 0}
        return {
            "source_name": row.source_name,
            "state": row.state,
            "failure_count": row.failure_count,
            "opened_at": row.opened_at.isoformat() if row.opened_at else None,
        }


def circuit_set(source_name: str, state: str, failure_count: int = 0) -> None:
    with session_scope() as s:
        row = s.get(DataSourceCircuit, source_name)
        if row is None:
            row = DataSourceCircuit(source_name=source_name)
            s.add(row)
        row.state = state
        row.failure_count = failure_count
        row.opened_at = _now() if state == "open" else row.opened_at
        s.commit()


# ---------------------------------------------------------------------------
# quant_task (SQLite 降级队列的直查辅助, 给监控 API 用)
# ---------------------------------------------------------------------------

def count_tasks(status: str, queue_name: Optional[str] = None) -> int:
    with session_scope() as s:
        q = s.query(QuantTask).filter(QuantTask.status == status)
        if queue_name:
            q = q.filter(QuantTask.queue_name == queue_name)
        return q.count()
