# TradingAgents/graph/propagation.py

from typing import Dict, Any, List, Optional
from ai_stock.agents.utils.agent_states import (
    AgentState,
    InvestDebateState,
    RiskDebateState,
    ANALYST_CHANNEL_KEYS,
)


class Propagator:
    """Handles state initialization and propagation through the graph."""

    def __init__(self, max_recur_limit=100):
        """Initialize with configuration parameters."""
        self.max_recur_limit = max_recur_limit

    def create_initial_state(
        self,
        company_name: str,
        trade_date: str,
        past_context: str = "",
        industry_heatmap: str = "",
        hot_sector_stocks: str = "",
    ) -> Dict[str, Any]:
        """Create the initial state for the agent graph.

        ``industry_heatmap`` / ``hot_sector_stocks`` carry the latest industry
        board (行业榜) context so analysts can judge the stock's sector-beta
        environment. Both must be initialized (possibly empty) because the
        analyst prompts read them from state.
        """
        initial = {
            "messages": [("human", company_name)],
            "company_of_interest": company_name,
            "trade_date": str(trade_date),
            "past_context": past_context,
            "industry_heatmap": industry_heatmap,
            "hot_sector_stocks": hot_sector_stocks,
            "investment_debate_state": InvestDebateState(
                {
                    "bull_history": "",
                    "bear_history": "",
                    "history": "",
                    "current_response": "",
                    "judge_decision": "",
                    "count": 0,
                }
            ),
            "risk_debate_state": RiskDebateState(
                {
                    "aggressive_history": "",
                    "conservative_history": "",
                    "neutral_history": "",
                    "history": "",
                    "latest_speaker": "",
                    "current_aggressive_response": "",
                    "current_conservative_response": "",
                    "current_neutral_response": "",
                    "judge_decision": "",
                    "count": 0,
                }
            ),
            "market_report": "",
            "fundamentals_report": "",
            "sentiment_report": "",
            "news_report": "",
            "policy_report": "",
            "hot_money_report": "",
        }
        # O1 并行图：每个分析师的私有通道从与主通道相同的起点出发，
        # 使并行/串行两种模式下分析师看到的输入完全一致。
        # （add_messages 拒绝 None 初始值，必须显式初始化。）
        for _channel in ANALYST_CHANNEL_KEYS.values():
            initial[_channel] = [("human", company_name)]
        return initial

    def get_graph_args(self, callbacks: Optional[List] = None) -> Dict[str, Any]:
        """Get arguments for the graph invocation.

        Args:
            callbacks: Optional list of callback handlers for tool execution tracking.
                       Note: LLM callbacks are handled separately via LLM constructor.
        """
        config = {"recursion_limit": self.max_recur_limit}
        if callbacks:
            config["callbacks"] = callbacks
        return {
            "stream_mode": "values",
            "config": config,
        }
