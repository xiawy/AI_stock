"""选股集群重构冒烟检查: FSM 顺序 / handler 注册 / 数据层接口可达."""
import os
import tempfile

os.environ["QUANT_DB_URL"] = f"sqlite:///{tempfile.gettempdir()}/quant_smoke_tmp.db"

from ai_stock.quant.orchestrator import FLOW_DEFINITIONS

steps = [s["step"] for s in FLOW_DEFINITIONS["selection"]["steps"]]
print("FSM steps:", steps)
assert steps == [
    "macro_event", "theme_radar", "limit_up_monitor", "industry_scan",
    "stock_selection", "confidence_maintain",
]  # 优化点 1: 深度分析合并入 stock_selection; 旁路增强: theme_radar 前置 + confidence_maintain 末置

from ai_stock.quant.agents import build_handlers
from ai_stock.quant.config import SELECTION_QUEUE

handlers = build_handlers()[SELECTION_QUEUE]
print("selection handlers:", sorted(handlers.keys()))
assert "macro_event" in handlers

from ai_stock.quant.agents.selection import (
    STAGE_DEFAULT_PRIORITY,
    StockDeepEval,
    StockDeepEvalList,
)
from ai_stock.quant.agents.macro_event import MacroEvent, MacroEventReport
from ai_stock.quant.event_industry_kb import match_industries, reconcile

print("stage_default_priority:", STAGE_DEFAULT_PRIORITY)
assert MacroEvent(title="t", level="global", impact_industries=["x"]).influence_score == 5.0
assert StockDeepEval(code="600000", confidence=7.0).bull_factors == []
assert "半导体" in match_industries("国产算力芯片重大突破")
assert reconcile(["半导体"], ["半导体", "通信设备"]) == ["半导体"]

from ai_stock.dataflows.pipeline_data import get_board_constituents  # noqa: F401
from ai_stock.quant.data_service import DataService

for name in ("get_hot_news", "get_all_industries", "get_industry_detail", "get_industry_stocks"):
    assert hasattr(DataService, name), name
print("data service interfaces OK")
print("SMOKE PASSED")
