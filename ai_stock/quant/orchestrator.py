"""Orchestrator FSM flow engine (设计文档 §8).

核心能力:
- 流程超时熔断: ``running`` 状态超过 ``timeout_seconds`` 无进展 → failed + 告警
  (以每步派发时刻为起点独立计时, 慢步骤不会被累计时长误杀; 事件到达视为进度)
- 失败 Fallback 降级: ``allow_fallback=True`` 时子任务失败不终止流程, 读取上
  一次成功 flow 的同步骤输出作为降级数据继续运行 (同时 WARNING 告警)
- 重启恢复: 启动时扫描 running flow, 超时的标记失败, 其余重新驱动当前步骤
  (结合 consumer_heartbeat 判断对应 Agent 进程存活)
- 事件去重: step_done 事件 (flow_id, step) 内存短时去重, 重复事件直接丢弃
- 严格状态流转: 预定义状态迁移表, 非法跳转直接置 flow 失败并告警

流程驱动模式: 每一步 Agent 完成后调用 ``report_step`` 向 orchestrator 队列
投递事件; Orchestrator 校验顺序并触发下一步任务入业务队列 (§8.2).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional

from . import db_ops
from .config import (
    BUY_QUEUE,
    FLOW_TIMEOUTS,
    HOLD_QUEUE,
    ORCHESTRATOR_QUEUE,
    SELECTION_QUEUE,
)
from .mq import enqueue_task

logger = logging.getLogger(__name__)

# 事件任务类型 (orchestrator 队列)
STEP_DONE_EVENT = "step_done"
STEP_FAILED_EVENT = "step_failed"

# 事件去重窗口 (秒): 同 (flow_id, step) 在窗口内重复到达直接丢弃
EVENT_DEDUPE_SECONDS = 600
EVENT_DEDUPE_MAX_ENTRIES = 4096


# ---------------------------------------------------------------------------
# Flow definitions (§8.2) — 步骤序列 + 每步投递的队列/任务类型
# ---------------------------------------------------------------------------

FLOW_DEFINITIONS: dict[str, dict] = {
    "selection": {
        "description": (
            "选股流程: 宏观事件解析 → 主题雷达 → 涨停潮监控 → 行业生命周期定位 → "
            "个股精选+深度分析(合并, 直接入自选池) → 动态置信度维护"
        ),
        "steps": (
            {"step": "macro_event", "queue": SELECTION_QUEUE, "task_type": "macro_event"},
            # 主题雷达前置: 微观异动扫描, radar_alerts 供 industry_scan 候选池注入 (旁路)
            {"step": "theme_radar", "queue": SELECTION_QUEUE, "task_type": "theme_radar"},
            # 涨停潮监控前置: 其 waves 结果供 industry_scan 阶段修正/排序引用 (§7.1.1)
            {"step": "limit_up_monitor", "queue": SELECTION_QUEUE, "task_type": "limit_up_monitor"},
            {"step": "industry_scan", "queue": SELECTION_QUEUE, "task_type": "industry_scan"},
            # 优化点 1: 深度分析合并入选股步, 无独立 deep_analysis 步
            {"step": "stock_selection", "queue": SELECTION_QUEUE, "task_type": "stock_selection"},
            # 动态置信度维护官: 选股末步, 衰减/增强/鱼尾标记/衰竭踢出 (旁路)
            {"step": "confidence_maintain", "queue": SELECTION_QUEUE, "task_type": "confidence_maintain"},
        ),
    },
    "buy": {
        "description": "单股票买入: 自选池清理 → 崩塌复核 → 技术信号 → 催化事件 → 二次验证 → 建仓",
        "steps": (
            # 自选池清理官前置: 置信度衰竭/鱼尾硬性过滤 (不经 LLM), 不通过则终止流程
            {"step": "watchlist_kicker", "queue": BUY_QUEUE, "task_type": "watchlist_kicker"},
            {"step": "logic_collapse_check", "queue": BUY_QUEUE, "task_type": "logic_collapse_check"},
            {"step": "tech_signal", "queue": BUY_QUEUE, "task_type": "tech_signal"},
            {"step": "catalyst_event", "queue": BUY_QUEUE, "task_type": "catalyst_event"},
            {"step": "second_verification", "queue": BUY_QUEUE, "task_type": "second_verification"},
            {"step": "position_open", "queue": BUY_QUEUE, "task_type": "position_open"},
        ),
    },
    "hold": {
        "description": "持仓维护: 走势跟踪 → 加仓决策 → 减仓决策 → 执行与规划更新",
        "steps": (
            {"step": "trend_tracking", "queue": HOLD_QUEUE, "task_type": "trend_tracking"},
            {"step": "add_position_decision", "queue": HOLD_QUEUE, "task_type": "add_position_decision"},
            {"step": "reduce_position_decision", "queue": HOLD_QUEUE, "task_type": "reduce_position_decision"},
            {"step": "execution_update", "queue": HOLD_QUEUE, "task_type": "execution_update"},
        ),
    },
}

# 严格状态流转表: 终态 (completed/failed/timeout) 不可再迁移
VALID_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "running": {"running", "completed", "failed", "timeout"},
    "completed": set(),
    "failed": set(),
    "timeout": set(),
}

FINAL_STATUSES = {"completed", "failed", "timeout"}


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Orchestrator:
    """FSM 流程引擎: 消费 orchestrator 队列事件, 驱动多 Agent 流程."""

    def __init__(self):
        # 事件去重: (flow_id, step) -> monotonic ts
        self._dedupe: OrderedDict[tuple[str, str], float] = OrderedDict()

    # ------------------------------------------------------------------
    # 流程启动
    # ------------------------------------------------------------------

    def start_flow(
        self,
        flow_type: str,
        flow_id: Optional[str] = None,
        data: Optional[dict] = None,
        allow_fallback: bool = False,
        kickoff: bool = True,
    ) -> dict:
        """创建 flow_state 并投递第一步任务 (flow_id 幂等)."""
        if flow_type not in FLOW_DEFINITIONS:
            raise ValueError(f"未知流程类型: {flow_type}")
        flow_id = flow_id or f"{flow_type}_{uuid.uuid4().hex[:12]}"
        timeout = int(FLOW_TIMEOUTS.get(flow_type, 1800))
        created = db_ops.create_flow(
            flow_id, flow_type, timeout, allow_fallback, data,
        )
        if created is None:
            logger.debug("Flow %s 已存在 (幂等), 不重复驱动", flow_id)
            return db_ops.get_flow(flow_id) or {"flow_id": flow_id, "status": "unknown"}
        db_ops.log_decision(
            agent="orchestrator", decision="flow_start", flow_id=flow_id,
            reason=FLOW_DEFINITIONS[flow_type]["description"],
            detail={"flow_type": flow_type, "allow_fallback": allow_fallback},
        )
        if kickoff:
            self._dispatch_step(flow_id, flow_type, 0, created.get("data") or {})
        return created

    # ------------------------------------------------------------------
    # MQ handlers (注册到 orchestrator 队列消费者)
    # ------------------------------------------------------------------

    def handle_step_event(self, task: dict) -> dict:
        """处理 step_done / step_failed 事件 (统一 task_type=step_done)."""
        payload = task.get("payload", {})
        flow_id = payload.get("flow_id", "")
        flow_type = payload.get("flow_type", "")
        step = payload.get("step", "")
        event = payload.get("event", STEP_DONE_EVENT)
        result = payload.get("result") or {}
        error = payload.get("error", "")
        return self._on_step_event(
            flow_type, flow_id, step, event, result, error,
        )

    def handle_start_event(self, task: dict) -> dict:
        """处理 flow_start 事件 (跨进程启动流程)."""
        payload = task.get("payload", {})
        return self.start_flow(
            payload.get("flow_type", ""),
            flow_id=payload.get("flow_id"),
            data=payload.get("data"),
            allow_fallback=payload.get("allow_fallback", False),
        )

    def get_handlers(self) -> dict:
        """供 TaskConsumer 注册: orchestrator 队列的 handler 表."""
        return {
            "step_done": self.handle_step_event,
            "flow_start": self.handle_start_event,
        }

    # ------------------------------------------------------------------
    # 事件处理核心
    # ------------------------------------------------------------------

    def _on_step_event(
        self,
        flow_type: str,
        flow_id: str,
        step: str,
        event: str,
        result: dict,
        error: str,
    ) -> dict:
        from .ops_monitor import ERROR, get_alerts

        if not flow_id or not step:
            logger.warning("Malformed step event ignored: %s", (flow_id, step))
            return {"action": "ignored", "reason": "malformed"}

        flow = db_ops.get_flow(flow_id)
        if flow is None:
            logger.warning("Step event for unknown flow %s (%s)", flow_id, step)
            return {"action": "ignored", "reason": "flow_not_found"}
        flow_type = flow_type or flow.get("flow_type", "")
        steps = FLOW_DEFINITIONS.get(flow_type, {}).get("steps", ())

        # 终态校验: 已结束的 flow 丢弃后续事件
        if flow.get("status") in FINAL_STATUSES:
            logger.debug(
                "Flow %s already %s, drop event %s", flow_id, flow["status"], step,
            )
            return {"action": "ignored", "reason": f"flow_{flow['status']}"}

        # 事件去重 (§8.1): (flow_id, step) 短时窗口内重复直接丢弃
        if self._is_duplicate(flow_id, step):
            return {"action": "ignored", "reason": "duplicate_event"}

        # 严格顺序校验: step 必须是「下一个待执行步骤」
        completed = list(flow.get("completed_steps", []))
        if not steps:
            self._fail_flow(flow, "failed", f"未知流程类型 {flow_type} 的事件")
            return {"action": "failed", "reason": "unknown_flow_type"}
        if step in completed:
            # 已完成步骤的重复事件 (跨去重窗口), 忽略
            return {"action": "ignored", "reason": "step_already_completed"}
        next_index = len(completed)
        expected = steps[next_index]["step"] if next_index < len(steps) else None
        if step != expected:
            # 非法顺序跳转 → flow 失败 + 告警 (§8.1 严格状态流转)
            get_alerts().emit(
                ERROR, "flow_invalid_transition",
                f"flow {flow_id} 非法步骤跳转: 期望 {expected}, 收到 {step}",
            )
            self._fail_flow(
                flow, "failed",
                f"非法步骤顺序: 期望 {expected}, 收到 {step}",
            )
            return {"action": "failed", "reason": "invalid_transition"}

        # 分支: 步骤失败 → fallback 降级 / flow failed
        if event == STEP_FAILED_EVENT:
            return self._handle_step_failed(flow, steps[next_index], error)

        # 正常完成: 累积关键结果到 flow.data, 推进下一步 (§8.1)
        completed.append(step)
        data = dict(flow.get("data") or {})
        data[step] = result
        # 业务提前终止 (逻辑崩塌移除/风控拒绝等) — flow 完成而非失败,
        # 不再派发后续步骤
        if result.get("abort"):
            abort_reason = str(result.get("abort_reason", ""))[:500]
            db_ops.update_flow(
                flow_id, status="completed", current_step=step,
                completed_steps=completed, data=data,
            )
            db_ops.log_decision(
                agent="orchestrator", decision="flow_aborted", flow_id=flow_id,
                reason=abort_reason, detail={"aborted_at_step": step},
            )
            logger.info("Flow %s aborted at step %s: %s", flow_id, step, abort_reason)
            return {"action": "aborted", "flow_id": flow_id, "reason": abort_reason}
        if next_index + 1 >= len(steps):
            db_ops.update_flow(
                flow_id, status="completed", current_step=step,
                completed_steps=completed, data=data,
            )
            db_ops.log_decision(
                agent="orchestrator", decision="flow_completed", flow_id=flow_id,
                reason=f"{flow_type} 流程全部步骤完成",
                detail={"completed_steps": completed},
            )
            logger.info("Flow %s (%s) completed", flow_id, flow_type)
            return {"action": "completed", "flow_id": flow_id}

        db_ops.update_flow(
            flow_id, current_step=step, completed_steps=completed, data=data,
        )
        self._dispatch_step(flow_id, flow_type, next_index + 1, data)
        return {
            "action": "advanced",
            "flow_id": flow_id,
            "next_step": steps[next_index + 1]["step"],
        }

    def _handle_step_failed(self, flow: dict, step_def: dict, error: str) -> dict:
        """子任务执行失败: allow_fallback 降级继续, 否则 flow failed (§8.1)."""
        from .ops_monitor import ERROR, WARNING, get_alerts

        flow_id = flow["flow_id"]
        flow_type = flow.get("flow_type", "")
        step = step_def["step"]

        if not flow.get("allow_fallback"):
            get_alerts().emit(
                ERROR, "flow_step_failed",
                f"flow {flow_id} 步骤 {step} 失败且未开启降级: {error}",
            )
            self._fail_flow(flow, "failed", f"步骤 {step} 失败: {error}")
            return {"action": "failed", "reason": error[:200]}

        # Fallback: 读取上一次成功 flow 的同步骤输出作为降级数据
        last = db_ops.get_last_completed_flow(flow_type)
        fallback_result = (last or {}).get("data", {}).get(step)
        if fallback_result is None:
            get_alerts().emit(
                ERROR, "flow_fallback_miss",
                f"flow {flow_id} 步骤 {step} 失败且无历史降级数据: {error}",
            )
            self._fail_flow(flow, "failed", f"步骤 {step} 失败且无降级数据: {error}")
            return {"action": "failed", "reason": "no_fallback_data"}

        get_alerts().emit(
            WARNING, "flow_fallback_used",
            f"flow {flow_id} 步骤 {step} 失败, 使用上一次成功流程输出降级继续: {error}",
        )
        completed = list(flow.get("completed_steps", []))
        completed.append(step)
        data = dict(flow.get("data") or {})
        data[step] = {
            "fallback": True,
            "fallback_error": error[:500],
            "result": fallback_result,
        }
        steps = FLOW_DEFINITIONS[flow_type]["steps"]
        if len(completed) >= len(steps):
            db_ops.update_flow(
                flow_id, status="completed", current_step=step,
                completed_steps=completed, data=data,
            )
            return {"action": "completed_fallback", "flow_id": flow_id}
        db_ops.update_flow(
            flow_id, current_step=step, completed_steps=completed, data=data,
        )
        self._dispatch_step(flow_id, flow_type, len(completed), data)
        return {
            "action": "advanced_fallback",
            "flow_id": flow_id,
            "next_step": steps[len(completed)]["step"],
        }

    # ------------------------------------------------------------------
    # 步骤派发
    # ------------------------------------------------------------------

    def _dispatch_step(
        self,
        flow_id: str,
        flow_type: str,
        step_index: int,
        flow_data: dict,
        idem_suffix: str = "",
    ) -> tuple[str, bool]:
        """把第 step_index 步任务投递到对应业务队列 (幂等)."""
        steps = FLOW_DEFINITIONS[flow_type]["steps"]
        step_def = steps[step_index]
        payload = {
            "flow_id": flow_id,
            "flow_type": flow_type,
            "step": step_def["step"],
            "step_index": step_index,
            # 剥离下划线前缀内部字段 (如 _step_started_at), 避免引擎元数据泄漏给下游
            "context": {k: v for k, v in flow_data.items() if not k.startswith("_")},
        }
        task_id, created = enqueue_task(
            step_def["queue"], step_def["task_type"], payload,
            idempotent_key=f"{flow_id}_step{step_index}_{step_def['step']}{idem_suffix}",
            priority=2,
        )
        if created:
            data = dict(flow_data)
            # 步骤级超时起点: 每步以自身派发时刻为起点独立计时 (§8.1),
            # slow-but-successful 步骤 (多轮 LLM) 不会被全局累计时长误杀。
            data["_step_started_at"] = datetime.now(timezone.utc).isoformat()
            db_ops.update_flow(flow_id, current_step=step_def["step"], data=data)
            logger.info(
                "Flow %s step [%d/%d] %s dispatched -> %s",
                flow_id, step_index + 1, len(steps), step_def["step"], step_def["queue"],
            )
        return task_id, created

    # ------------------------------------------------------------------
    # 超时熔断 / 重启恢复
    # ------------------------------------------------------------------

    @staticmethod
    def _is_timed_out(flow: dict) -> bool:
        timeout = int(flow.get("timeout_seconds") or 0)
        if timeout <= 0:
            return False
        # 超时以「最近一次步骤派发」为起点按步计预算 (每步最多 timeout_seconds),
        # 而不是按 flow 整体累计时长: 合法长步骤 (选股深分析多轮 LLM) 只占用
        # 自己步骤的预算, 不会被全局时间桶误判超时。旧数据无 _step_started_at
        # 时退回 updated_at / created_at。
        step_started = _parse_iso((flow.get("data") or {}).get("_step_started_at"))
        ref = (
            step_started
            or _parse_iso(flow.get("updated_at"))
            or _parse_iso(flow.get("created_at"))
        )
        if ref is None:
            return False
        return (datetime.now(timezone.utc) - ref).total_seconds() > timeout

    def _fail_flow(self, flow: dict, status: str, reason: str) -> None:
        from .ops_monitor import ERROR, get_alerts

        flow_id = flow["flow_id"]
        current = flow.get("status", "running")
        # 严格状态流转: 非法跳转直接置 failed 并告警
        if status not in VALID_STATUS_TRANSITIONS.get(current, set()):
            get_alerts().emit(
                ERROR, "flow_invalid_transition",
                f"flow {flow_id} 非法状态跳转 {current} -> {status}, 强制置 failed",
            )
            status = "failed"
        db_ops.update_flow(flow_id, status=status, error=reason)
        db_ops.log_decision(
            agent="orchestrator", decision=f"flow_{status}", flow_id=flow_id,
            reason=reason,
        )
        get_alerts().emit(
            ERROR, f"flow_{status}",
            f"flow {flow_id} ({flow.get('flow_type')}) {status}: {reason}",
        )

    def check_flow_timeouts(self) -> list[str]:
        """扫描全部 running flow 的超时熔断 (供周期巡检/每日报告调用)."""
        timed_out: list[str] = []
        for flow in db_ops.get_running_flows():
            if self._is_timed_out(flow):
                self._fail_flow(
                    flow, "timeout",
                    f"流程超时 ({flow.get('timeout_seconds')}s 无进展)",
                )
                timed_out.append(flow["flow_id"])
        return timed_out

    def recover_on_startup(self) -> dict:
        """重启恢复 (§8.1): 超时 flow 标记失败, 其余 running flow 重新驱动当前步骤.

        结合 consumer_heartbeat: 对应队列无活跃消费者时仅告警提示部署问题,
        不阻塞恢复 (任务会在消费者启动后被正常消费).
        """
        from .ops_monitor import WARNING, get_alerts

        result: dict = {"timed_out": [], "re_driven": [], "completed": [], "no_consumer": []}
        # 活跃消费者 = 心跳新鲜的队列集合 (判断 Agent 进程存活)
        alive_queues: set[str] = {
            row["queue_name"] for row in self._all_heartbeats()
        }

        for flow in db_ops.get_running_flows():
            flow_id = flow["flow_id"]
            flow_type = flow.get("flow_type", "")
            if flow_type not in FLOW_DEFINITIONS:
                continue
            if self._is_timed_out(flow):
                self._fail_flow(
                    flow, "timeout",
                    f"重启恢复: 流程超时 ({flow.get('timeout_seconds')}s 无进展)",
                )
                result["timed_out"].append(flow_id)
                continue

            completed = list(flow.get("completed_steps", []))
            steps = FLOW_DEFINITIONS[flow_type]["steps"]
            next_index = len(completed)
            if next_index >= len(steps):
                # 全部步骤完成但事件丢失 → 直接完成
                db_ops.update_flow(flow_id, status="completed")
                result["completed"].append(flow_id)
                continue

            step_def = steps[next_index]
            if step_def["queue"] not in alive_queues:
                result["no_consumer"].append(flow_id)
                get_alerts().emit(
                    WARNING, "recover_no_consumer",
                    f"flow {flow_id} 待执行步骤 {step_def['step']} 所在队列 "
                    f"{step_def['queue']} 暂无活跃消费者, 任务将等待",
                )
            # 重新驱动当前步骤 (新幂等 key 强制补发; Agent 业务幂等保证安全)
            self._dispatch_step(
                flow_id, flow_type, next_index, flow.get("data") or {},
                idem_suffix=f"_recover{int(time.time())}",
            )
            result["re_driven"].append(flow_id)

        if any(result[k] for k in ("timed_out", "re_driven", "completed")):
            logger.info(
                "Startup recovery: timed_out=%d, re_driven=%d, completed=%d",
                len(result["timed_out"]), len(result["re_driven"]),
                len(result["completed"]),
            )
        return result

    @staticmethod
    def _all_heartbeats() -> list[dict]:
        """全部心跳 (活跃队列判定)."""
        import datetime as _dt

        from .db import session_scope
        from .db_models import ConsumerHeartbeat

        cutoff = _dt.datetime.now(timezone.utc) - _dt.timedelta(seconds=180)
        try:
            with session_scope() as s:
                rows = s.query(ConsumerHeartbeat).filter(
                    ConsumerHeartbeat.last_beat >= cutoff,
                ).all()
                return [
                    {
                        "consumer_id": r.consumer_id,
                        "queue_name": r.queue_name,
                        "last_beat": r.last_beat,
                    }
                    for r in rows
                ]
        except Exception as exc:
            logger.warning("heartbeat query failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # 事件去重
    # ------------------------------------------------------------------

    def _is_duplicate(self, flow_id: str, step: str) -> bool:
        key = (flow_id, step)
        now = time.monotonic()
        # 惰性清理过期项
        while self._dedupe:
            oldest_key, oldest_ts = next(iter(self._dedupe.items()))
            if now - oldest_ts > EVENT_DEDUPE_SECONDS:
                self._dedupe.popitem(last=False)
            else:
                break
        if key in self._dedupe:
            return True
        self._dedupe[key] = now
        if len(self._dedupe) > EVENT_DEDUPE_MAX_ENTRIES:
            self._dedupe.popitem(last=False)
        return False


# ---------------------------------------------------------------------------
# Agent-side helpers
# ---------------------------------------------------------------------------

def report_step(
    flow_type: str,
    flow_id: str,
    step: str,
    result: Optional[dict] = None,
    error: str = "",
    failed: bool = False,
) -> tuple[str, bool]:
    """Agent 完成步骤后向 Orchestrator 投递事件 (幂等).

    - 正常完成: ``failed=False``, result 携带关键结果 (存入 flow.data)
    - 业务放弃/数据缺失: ``failed=True`` + error — 由 Orchestrator 决定
      fallback 降级或终止流程. 注意: 业务结论「不买入」不是失败, 走正常
      result (decision=rejected) 路径.
    """
    payload = {
        "flow_type": flow_type,
        "flow_id": flow_id,
        "step": step,
        "event": STEP_FAILED_EVENT if failed else STEP_DONE_EVENT,
        "result": result or {},
        "error": error or "",
    }
    suffix = "failed" if failed else "done"
    return enqueue_task(
        ORCHESTRATOR_QUEUE, "step_done", payload,
        idempotent_key=f"evt_{flow_id}_{step}_{suffix}",
        priority=5,
    )


_orchestrator: Optional[Orchestrator] = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator
