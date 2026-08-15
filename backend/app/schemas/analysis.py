"""Analysis / task Pydantic schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AnalysisStartRequest(BaseModel):
    """Body of POST /api/analysis/start — mirrors the dashboard analysis form.

    LLM configuration (provider / model / base_url) is read from .env via
    DEFAULT_CONFIG — no per-request override needed.
    """

    ticker: str = Field(min_length=1, max_length=32, description="6位代码或中文全称，如 300750 / 宁德时代")
    trade_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="分析日期 YYYY-MM-DD")
    lookback_days: int | None = Field(
        default=None, ge=5, le=750, description="技术分析回溯天数（默认本月第一天）"
    )
    # fresh=True → 清除断点从头分析；False → 从上次断点续跑
    fresh: bool = True


class TaskStatusResponse(BaseModel):
    task_id: str
    ticker: str
    trade_date: str
    is_running: bool
    is_complete: bool
    is_paused: bool
    stop_requested: bool
    error: str | None = None
    signal: str = ""
    elapsed: float = 0.0
    current_stage: str = ""
    stages: list[dict[str, Any]] = Field(default_factory=list)
    llm_calls: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


class TaskCreatedResponse(BaseModel):
    task_id: str
    ticker: str
    trade_date: str


class AnalysisResultResponse(BaseModel):
    task_id: str
    ticker: str
    stock_label: str
    trade_date: str
    signal: str
    elapsed: float | None = None
    final_state: dict[str, Any] = Field(default_factory=dict)
