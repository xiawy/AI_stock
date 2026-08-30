"""ORM models for the quant trading subsystem (设计文档 §13.2).

13 张表:
- ``quant_flow_state``        — FSM 流程状态/中间结果 (流程引擎核心)
- ``consumer_heartbeat``      — Agent 消费者心跳 (进程存活判定)
- ``stock_pool_optional``     — 自选观察池 (active/removed/expired)
- ``stock_pool_holding``      — 当前持仓 (含 T+1 禁卖与操作规划)
- ``trade_log``               — 全部成交记录
- ``agent_decision_log``      — 每一步 AI 决策日志 (完整审计)
- ``strategy_rules``          — 策略规则 (版本/灰度/样本外门槛)
- ``rule_test_case``          — 规则单元测试用例
- ``evolution_history``       — 自我进化调参历史 (三套数据集指标)
- ``quant_cache_data``        — 三级缓存的 SQLite 冷归档
- ``news_vectors``            — 新闻向量元数据 (对应 ChromaDB)
- ``data_source_circuit``     — 数据源熔断状态
- ``system_config``           — 全局配置 (global_trade_enable 等)
- ``quant_task``              — SQLite 降级消息队列
- ``quant_industry_board``    — 行业榜 (选股流程生命周期排序产出, 含龙头股)
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import QuantBase


def _now() -> datetime:
    return datetime.now(timezone.utc)


class QuantFlowState(QuantBase):
    """FSM 流程状态, Orchestrator 流程引擎核心."""

    __tablename__ = "quant_flow_state"

    flow_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    flow_type: Mapped[str] = mapped_column(String(32), nullable=False)  # selection/buy/hold
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    # running | completed | failed | timeout
    current_step: Mapped[str] = mapped_column(String(48), default="")
    completed_steps_json: Mapped[str] = mapped_column(Text, default="[]")
    data_json: Mapped[str] = mapped_column(Text, default="{}")
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    allow_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now,
    )

    def to_dict(self) -> dict:
        import json

        return {
            "flow_id": self.flow_id,
            "flow_type": self.flow_type,
            "status": self.status,
            "current_step": self.current_step,
            "completed_steps": json.loads(self.completed_steps_json or "[]"),
            "data": json.loads(self.data_json or "{}"),
            "timeout_seconds": self.timeout_seconds,
            "allow_fallback": self.allow_fallback,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ConsumerHeartbeat(QuantBase):
    """Agent 消费者心跳, 用于重启恢复时判断进程存活."""

    __tablename__ = "consumer_heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    consumer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    queue_name: Mapped[str] = mapped_column(String(32), nullable=False)
    last_beat: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")

    __table_args__ = (UniqueConstraint("consumer_id", name="uq_consumer"),)


class StockPoolOptional(QuantBase):
    """自选观察池 (设计文档 §2.4 动态维护)."""

    __tablename__ = "quant_stock_pool_optional"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    industry: Mapped[str] = mapped_column(String(64), default="")
    add_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    reason: Mapped[str] = mapped_column(Text, default="")           # 入选理由
    bull_factors: Mapped[str] = mapped_column(Text, default="")     # 主要利多(JSON list)
    bear_factors: Mapped[str] = mapped_column(Text, default="")     # 主要利空(JSON list)
    stage_judgement: Mapped[str] = mapped_column(Text, default="")  # 当前所处阶段
    rise_trigger: Mapped[str] = mapped_column(Text, default="")     # 上涨触发条件预测
    report: Mapped[str] = mapped_column(Text, default="")           # 完整分析报告
    risk_tags_json: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # active / removed / expired / bought
    status: Mapped[str] = mapped_column(String(16), default="active")
    observe_expire: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    remove_reason: Mapped[str] = mapped_column(Text, default="")
    remove_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        import json

        return {
            "symbol": self.symbol,
            "name": self.name,
            "industry": self.industry,
            "add_time": self.add_time.isoformat() if self.add_time else None,
            "reason": self.reason,
            "bull_factors": json.loads(self.bull_factors or "[]"),
            "bear_factors": json.loads(self.bear_factors or "[]"),
            "stage_judgement": self.stage_judgement,
            "rise_trigger": self.rise_trigger,
            "report": self.report,
            "risk_tags": json.loads(self.risk_tags_json or "[]"),
            "confidence": self.confidence,
            "status": self.status,
            "observe_expire": self.observe_expire.isoformat() if self.observe_expire else None,
            "remove_reason": self.remove_reason,
            "remove_time": self.remove_time.isoformat() if self.remove_time else None,
        }


class StockPoolHolding(QuantBase):
    """当前持仓池 (按用户隔离; user_id='system' 为引擎系统账户)."""

    __tablename__ = "quant_stock_pool_holding"

    user_id: Mapped[str] = mapped_column(String(32), primary_key=True, default="system")
    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    quantity: Mapped[int] = mapped_column(Integer, default=0)         # 总持仓
    available_quantity: Mapped[int] = mapped_column(Integer, default=0)  # 可卖(T+1后)
    cost_price: Mapped[float] = mapped_column(Float, default=0.0)
    buy_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    entry_reason: Mapped[str] = mapped_column(Text, default="")
    plan_json: Mapped[str] = mapped_column(Text, default="{}")        # 操作规划
    predicted_path_json: Mapped[str] = mapped_column(Text, default="{}")  # 买入时走势预测
    stop_loss: Mapped[float] = mapped_column(Float, default=0.0)
    take_profit: Mapped[float] = mapped_column(Float, default=0.0)
    last_review_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    cannot_sell_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        import json

        return {
            "user_id": self.user_id,
            "symbol": self.symbol,
            "name": self.name,
            "quantity": self.quantity,
            "available_quantity": self.available_quantity,
            "cost_price": self.cost_price,
            "buy_time": self.buy_time.isoformat() if self.buy_time else None,
            "entry_reason": self.entry_reason,
            "plan": json.loads(self.plan_json or "{}"),
            "predicted_path": json.loads(self.predicted_path_json or "{}"),
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "last_review_time": (
                self.last_review_time.isoformat() if self.last_review_time else None
            ),
            "cannot_sell_until": (
                self.cannot_sell_until.isoformat() if self.cannot_sell_until else None
            ),
        }


class TradeLog(QuantBase):
    """全部成交/委托记录 (按用户隔离; 卖出时记录该笔盈亏)."""

    __tablename__ = "quant_trade_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(32), default="system", index=True)
    order_id: Mapped[str] = mapped_column(String(64), default="")
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    side: Mapped[str] = mapped_column(String(8), nullable=False)   # buy / sell
    price: Mapped[float] = mapped_column(Float, default=0.0)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="filled")
    # filled / partially_filled / cancelled / rejected / pending
    reason: Mapped[str] = mapped_column(Text, default="")            # 决策理由
    broker: Mapped[str] = mapped_column(String(32), default="simulated")
    # 卖出时的单笔盈亏审计: 参考成本 / 已实现盈亏(扣卖出费) / 盈亏比例
    cost_price: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    trade_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "name": self.name,
            "side": self.side,
            "price": self.price,
            "quantity": self.quantity,
            "amount": self.amount,
            "fee": self.fee,
            "status": self.status,
            "reason": self.reason,
            "broker": self.broker,
            "cost_price": self.cost_price,
            "realized_pnl": self.realized_pnl,
            "pnl_pct": self.pnl_pct,
            "trade_time": self.trade_time.isoformat() if self.trade_time else None,
        }


class UserAccount(QuantBase):
    """用户模拟交易账户 — 每用户独立起始资金/现金/状态."""

    __tablename__ = "quant_user_account"

    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    initial_capital: Mapped[float] = mapped_column(Float, default=0.0)
    cash_balance: Mapped[float] = mapped_column(Float, default=0.0)
    # active / frozen (风控冻结仅允许卖出)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now,
    )

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "initial_capital": self.initial_capital,
            "cash_balance": self.cash_balance,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AgentDecisionLog(QuantBase):
    """每一步 AI 决策日志, 完整审计."""

    __tablename__ = "quant_agent_decision_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    agent: Mapped[str] = mapped_column(String(48), nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), default="")
    flow_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    symbol: Mapped[str] = mapped_column(String(16), default="", index=True)
    decision: Mapped[str] = mapped_column(String(32), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    detail_json: Mapped[str] = mapped_column(Text, default="{}")

    def to_dict(self) -> dict:
        import json

        return {
            "id": self.id,
            "ts": self.ts.isoformat() if self.ts else None,
            "agent": self.agent,
            "task_id": self.task_id,
            "flow_id": self.flow_id,
            "symbol": self.symbol,
            "decision": self.decision,
            "reason": self.reason,
            "detail": json.loads(self.detail_json or "{}"),
        }


class StrategyRule(QuantBase):
    """策略规则配置 (simpleeval 表达式 + 版本/灰度)."""

    __tablename__ = "quant_strategy_rules"

    rule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(Text, default="")
    condition: Mapped[str] = mapped_column(Text, nullable=False)  # 表达式
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=5)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    test_case_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    gray_scale: Mapped[bool] = mapped_column(Boolean, default=False)  # 灰度=仅模拟盘
    min_sample_out_perf: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now,
    )

    def to_dict(self) -> dict:
        import json

        return {
            "rule_id": self.rule_id,
            "description": self.description,
            "condition": self.condition,
            "action": self.action,
            "priority": self.priority,
            "enabled": self.enabled,
            "version": self.version,
            "test_case_ids": json.loads(self.test_case_ids_json or "[]"),
            "gray_scale": self.gray_scale,
            "min_sample_out_perf": self.min_sample_out_perf,
        }


class RuleTestCase(QuantBase):
    """规则单元测试用例 — 新规则上线强制流水线第一步."""

    __tablename__ = "quant_rule_test_case"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    case_name: Mapped[str] = mapped_column(String(128), default="")
    input_json: Mapped[str] = mapped_column(Text, default="{}")     # 表达式上下文
    expected: Mapped[bool] = mapped_column(Boolean, default=True)   # 期望触发与否
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    def to_dict(self) -> dict:
        import json

        return {
            "id": self.id,
            "rule_id": self.rule_id,
            "case_name": self.case_name,
            "input": json.loads(self.input_json or "{}"),
            "expected": self.expected,
            "enabled": self.enabled,
        }


class EvolutionHistory(QuantBase):
    """自我进化调参历史 — 训练/验证/样本外三套指标, 方便审计."""

    __tablename__ = "quant_evolution_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    rule_id: Mapped[str] = mapped_column(String(64), default="")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    train_metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    valid_metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    oos_metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    # draft / test_failed / oos_failed / pending_review / approved / rejected
    status: Mapped[str] = mapped_column(String(24), default="draft")
    reviewer_note: Mapped[str] = mapped_column(Text, default="")

    def to_dict(self) -> dict:
        import json

        return {
            "id": self.id,
            "ts": self.ts.isoformat() if self.ts else None,
            "rule_id": self.rule_id,
            "params": json.loads(self.params_json or "{}"),
            "train_metrics": json.loads(self.train_metrics_json or "{}"),
            "valid_metrics": json.loads(self.valid_metrics_json or "{}"),
            "oos_metrics": json.loads(self.oos_metrics_json or "{}"),
            "status": self.status,
            "reviewer_note": self.reviewer_note,
        }


class QuantCacheData(QuantBase):
    """三级缓存的 SQLite 冷归档 (Redis 故障可从此重建)."""

    __tablename__ = "quant_cache_data"

    cache_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    category: Mapped[str] = mapped_column(String(32), default="", index=True)
    payload: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class NewsVectorMeta(QuantBase):
    """新闻向量元数据, 与 ChromaDB collection 一一对应."""

    __tablename__ = "quant_news_vectors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    news_hash: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(64), default="")
    pub_time: Mapped[str] = mapped_column(String(32), default="")
    symbol: Mapped[str] = mapped_column(String(16), default="", index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("news_hash", name="uq_news_hash"),)


class DataSourceCircuit(QuantBase):
    """数据源熔断状态 (closed / open / half_open)."""

    __tablename__ = "quant_data_source_circuit"

    source_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="closed")
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now,
    )


class SystemConfig(QuantBase):
    """系统全局配置 (global_trade_enable / 实盘权限参数等)."""

    __tablename__ = "quant_system_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")


class QuantTask(QuantBase):
    """SQLite 降级消息队列 (Redis 不可用时; 结构对齐 mq:task_meta Hash)."""

    __tablename__ = "quant_task"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    queue_name: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    task_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    # pending / delayed / processing / done / retry / failed / dead
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    last_error: Mapped[str] = mapped_column(Text, default="")
    consumer_id: Mapped[str] = mapped_column(String(64), default="")
    processing_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now,
    )

    __table_args__ = (
        Index("ix_quant_task_dispatch", "queue_name", "status", "available_at"),
    )

    def to_dict(self) -> dict:
        import json

        return {
            "task_id": self.task_id,
            "queue_name": self.queue_name,
            "task_type": self.task_type,
            "payload": json.loads(self.payload_json or "{}"),
            "status": self.status,
            "priority": self.priority,
            "available_at": (
                self.available_at.isoformat() if self.available_at else None
            ),
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "last_error": self.last_error,
            "consumer_id": self.consumer_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class QuantIndustryBoard(QuantBase):
    """行业榜 — 选股流程产出 (§7.1).

    数据源 = 筛选自选时生命周期定位排序后的前 ``INDUSTRY_BOARD_SIZE`` 个
    行业; 每行业附带前 ``LEADER_STOCKS_PER_BOARD`` 只龙头股 (存 JSON)。
    按 ``rank_date`` 每日覆盖写入, 天然支持按日期回查历史。
    """

    __tablename__ = "quant_industry_board"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rank_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    industry: Mapped[str] = mapped_column(String(64), default="")
    industry_code: Mapped[str] = mapped_column(String(16), default="")
    industry_level: Mapped[str] = mapped_column(String(16), default="")  # industry|concept
    stage: Mapped[str] = mapped_column(String(16), default="")          # 生命周期阶段
    event_tag: Mapped[str] = mapped_column(String(64), default="")      # 关联宏观事件标签
    heat_score: Mapped[float] = mapped_column(Float, default=0.0)       # 阶段优选级 (含涨停潮加分)
    change_pct: Mapped[float] = mapped_column(Float, nullable=True)     # 板块当日涨跌幅
    main_net_inflow: Mapped[float] = mapped_column(Float, nullable=True)  # 主力净流入 (元)
    leader_stocks_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("rank_date", "rank", name="uq_board_date_rank"),
    )

    def to_dict(self) -> dict:
        import json

        return {
            "id": self.id,
            "rank_date": self.rank_date,
            "rank": self.rank,
            "industry": self.industry,
            "industry_code": self.industry_code,
            "industry_level": self.industry_level,
            "stage": self.stage,
            "event_tag": self.event_tag,
            "heat_score": self.heat_score,
            "change_pct": self.change_pct,
            "main_net_inflow": self.main_net_inflow,
            "leader_stocks": json.loads(self.leader_stocks_json or "[]"),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
