"""共享数据工具层 — ai_stock 多 Agent 流水线与 quant 量化子系统共用.

每个工具分两层:
- ``fetch_*`` 纯函数内核: 返回 ``route_to_vendor`` 的原始结果 (文本或结构化
  list), 供 quant DataService 等程序化调用;
- ``@tool`` 封装 (原函数名): 保留 Annotated 参数描述与 LLM 友好的文档,
  供 langchain Agent 绑定使用.

所有取数统一经 ``ai_stock.dataflows.interface.route_to_vendor`` 路由,
享受供应商配置与降级链。
"""

from .core_stock_tools import fetch_stock_data, get_stock_data
from .technical_indicators_tools import fetch_indicators, get_indicators
from .fundamental_data_tools import (
    fetch_balance_sheet,
    fetch_cashflow,
    fetch_fundamentals,
    fetch_income_statement,
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_income_statement,
)
from .news_data_tools import (
    fetch_global_news,
    fetch_insider_transactions,
    fetch_news,
    get_global_news,
    get_insider_transactions,
    get_news,
)
from .signal_data_tools import (
    fetch_concept_blocks,
    fetch_dragon_tiger_board,
    fetch_fund_flow,
    fetch_hot_stocks,
    fetch_impact_news,
    fetch_industry_comparison,
    fetch_limit_up_stocks,
    fetch_lockup_expiry,
    fetch_northbound_flow,
    fetch_profit_forecast,
    get_concept_blocks,
    get_dragon_tiger_board,
    get_fund_flow,
    get_hot_stocks,
    get_impact_news,
    get_industry_comparison,
    get_limit_up_stocks,
    get_lockup_expiry,
    get_northbound_flow,
    get_profit_forecast,
)

__all__ = [
    # 纯函数内核 (程序化调用)
    "fetch_stock_data",
    "fetch_indicators",
    "fetch_fundamentals",
    "fetch_balance_sheet",
    "fetch_cashflow",
    "fetch_income_statement",
    "fetch_news",
    "fetch_global_news",
    "fetch_insider_transactions",
    "fetch_profit_forecast",
    "fetch_hot_stocks",
    "fetch_northbound_flow",
    "fetch_concept_blocks",
    "fetch_fund_flow",
    "fetch_dragon_tiger_board",
    "fetch_lockup_expiry",
    "fetch_industry_comparison",
    "fetch_impact_news",
    "fetch_limit_up_stocks",
    # @tool 封装 (LLM Agent 绑定)
    "get_stock_data",
    "get_indicators",
    "get_fundamentals",
    "get_balance_sheet",
    "get_cashflow",
    "get_income_statement",
    "get_news",
    "get_global_news",
    "get_insider_transactions",
    "get_profit_forecast",
    "get_hot_stocks",
    "get_northbound_flow",
    "get_concept_blocks",
    "get_fund_flow",
    "get_dragon_tiger_board",
    "get_lockup_expiry",
    "get_industry_comparison",
    "get_impact_news",
    "get_limit_up_stocks",
]
