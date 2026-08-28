"""Agent base class (设计文档 §7).

所有 Agent 都是队列消费者 handler — 处理完成后向 Orchestrator 投递
``report_step`` 事件. 基类提供:
- LLM 懒加载单例 (quick/deep 双模型, 复用 llm_clients factory)
- 决策日志快捷方法 (agent_decision_log 审计)
- flow payload 提取工具

异常约定:
- 系统性异常 (网络/程序错误) → handler 直接抛出, 交给 MQ 指数退避重试
- 业务性放弃 (数据不可用且无兜底) → ``report_step(failed=True)`` 由
  Orchestrator 决定 fallback 降级或终止流程
- 业务结论「不买/不选」不是失败, 走正常 result 路径 (decision=rejected)
"""

from __future__ import annotations

import logging
from typing import Optional

from .. import db_ops
from ..data_service import DataService, get_data_service
from ..llm_helper import QuantLLM, create_quant_llm
from ..orchestrator import report_step

logger = logging.getLogger(__name__)


class BaseAgent:
    """量化 Agent 基类: 数据服务 + LLM + 决策日志."""

    agent_name = "base"

    def __init__(self, data_service: Optional[DataService] = None):
        self.data = data_service or get_data_service()
        self._llm_holder: Optional[QuantLLM] = None

    # -- LLM (懒加载, 失败降级为不可用) --------------------------------------

    @property
    def llm(self) -> QuantLLM:
        if self._llm_holder is None:
            try:
                from ai_stock.default_config import DEFAULT_CONFIG

                self._llm_holder = create_quant_llm(DEFAULT_CONFIG)
            except Exception as exc:
                logger.warning("Agent %s LLM init failed: %s", self.agent_name, exc)
                self._llm_holder = QuantLLM(None, None)
        return self._llm_holder

    # -- 决策日志 --------------------------------------------------------------

    def log(
        self,
        decision: str,
        reason: str = "",
        symbol: str = "",
        task_id: str = "",
        flow_id: str = "",
        detail: dict | None = None,
    ) -> None:
        db_ops.log_decision(
            agent=self.agent_name, decision=decision, reason=reason,
            symbol=symbol, task_id=task_id, flow_id=flow_id, detail=detail,
        )

    # -- flow payload 提取 -------------------------------------------------------

    @staticmethod
    def extract_flow(payload: dict) -> tuple[str, str, str, dict]:
        """提取 (flow_id, flow_type, step, context)."""
        return (
            payload.get("flow_id", ""),
            payload.get("flow_type", ""),
            payload.get("step", ""),
            payload.get("context", {}) or {},
        )

    # -- 步骤上报 --------------------------------------------------------------

    def report(
        self,
        flow_type: str,
        flow_id: str,
        step: str,
        result: dict,
        error: str = "",
        failed: bool = False,
    ) -> None:
        """步骤结果上报 Orchestrator (幂等)."""
        if not flow_id:
            return
        report_step(flow_type, flow_id, step, result=result, error=error, failed=failed)
