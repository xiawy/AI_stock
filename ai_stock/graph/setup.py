# TradingAgents/graph/setup.py

import logging
from typing import Any, Dict, Optional
from pathlib import Path
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from ai_stock.agents import *
from ai_stock.agents.utils.agent_states import AgentState, ANALYST_CHANNEL_KEYS
from ai_stock.default_config import DEFAULT_CONFIG

from .conditional_logic import ConditionalLogic

logger = logging.getLogger(__name__)

# Lazy imports for evolution system (only loaded when enabled)
_evolution_imported = False
EvolutionWrapper = None
AgentMemorySystem = None
_shared_chroma_client = None

def _ensure_evolution_imports():
    global _evolution_imported, EvolutionWrapper, AgentMemorySystem
    if not _evolution_imported:
        from ai_stock.evolution.agent_wrapper import EvolutionWrapper as EW
        from ai_stock.evolution.memory_system import AgentMemorySystem as AMS
        EvolutionWrapper = EW
        AgentMemorySystem = AMS
        _evolution_imported = True

def _get_shared_chroma_client(base_dir):
    """Return a single shared Chroma PersistentClient for all agents.

    Avoids loading the ONNX embedding model 15 times (once per agent).
    """
    global _shared_chroma_client
    if _shared_chroma_client is None:
        import chromadb
        _shared_chroma_client = chromadb.PersistentClient(path=str(base_dir))
    return _shared_chroma_client


# 需要综合全局信息做决策的两个节点走 deep 档，其余走 quick 档。
# 这是**没有单独配置角色模型时**的默认分档，与原行为一致。
DEEP_ROLES = frozenset({"research_manager", "portfolio_manager"})

# 可以单独指定模型的角色（config["role_llms"] 的合法键）。
# 单列出来是为了把配错的角色名当场报出来，而不是静默忽略、让人以为配置生效了。
ROLE_KEYS = (
    "market", "social", "news", "fundamentals", "policy", "hot_money",
    "quality_gate", "bull", "bear", "research_manager", "trader",
    "risk_aggressive", "risk_neutral", "risk_conservative", "portfolio_manager",
)


# ---------------------------------------------------------------------------
# O1 并行化辅助：让分析师/工具节点读写各自的私有消息通道
# ---------------------------------------------------------------------------

def _channel_adapter(channel_key: str, node_fn):
    """Wrap an analyst node so it reads/writes its private message channel.

    入口：把私有通道内容映射到 ``messages`` 键喂给节点（analyst 与 evolution
    wrapper 只认 messages）；出口：节点产出的 messages 写回私有通道；若是
    最终报告（无 tool_calls），再同步一条到主通道，供进度展示/日志/下游
    调试使用。主通道的同步消息不会被其它分析师读到（它们只读各自通道），
    所以不会产生串行模式下的上下文串扰。
    """

    def wrapped(state):
        local = dict(state)
        local["messages"] = list(state.get(channel_key) or [])
        update = node_fn(local) or {}
        out = dict(update)
        msgs = out.pop("messages", None)
        if msgs is not None:
            out[channel_key] = msgs
            if msgs and not getattr(msgs[-1], "tool_calls", None):
                out["messages"] = [msgs[-1]]
        return out

    return wrapped


def _tool_channel_adapter(channel_key: str, tool_node):
    """Wrap a ToolNode so tool results land in the analyst's private channel."""

    def wrapped(state):
        local = dict(state)
        local["messages"] = list(state.get(channel_key) or [])
        update = tool_node.invoke(local) or {}
        out = dict(update)
        msgs = out.pop("messages", None)
        if msgs is not None:
            out[channel_key] = msgs
        return out

    return wrapped


def _channel_route(channel_key: str, tools_name: str, sink: str):
    """Route on the private channel's last message instead of ``messages``."""

    def route(state):
        msgs = state.get(channel_key) or []
        if msgs and getattr(msgs[-1], "tool_calls", None):
            return tools_name
        return sink

    return route


class GraphSetup:
    """Handles the setup and configuration of the agent graph."""

    def __init__(
        self,
        quick_thinking_llm: Any,
        deep_thinking_llm: Any,
        tool_nodes: Dict[str, ToolNode],
        conditional_logic: ConditionalLogic,
        resolve_llm=None,
        config: Optional[Dict[str, Any]] = None,
    ):
        """Initialize with required components.

        resolve_llm: 可选，`role -> llm | None` 的查表函数。返回 None 表示该角色
        没有单独配置，回落到 quick/deep 两档——不传就是完全的原行为（#39）。
        config: 可选，全局配置字典。用于进化层设置。
        """
        self.quick_thinking_llm = quick_thinking_llm
        self.deep_thinking_llm = deep_thinking_llm
        self.tool_nodes = tool_nodes
        self.conditional_logic = conditional_logic
        self._resolve_llm = resolve_llm
        self._config = config or DEFAULT_CONFIG

    def llm_for(self, role: str) -> Any:
        """取某个角色该用的 LLM。没单独配就回落到 quick/deep 两档。"""
        if self._resolve_llm is not None:
            llm = self._resolve_llm(role)
            if llm is not None:
                return llm
        return self.deep_thinking_llm if role in DEEP_ROLES else self.quick_thinking_llm

    def _wrap_evolution(self, role: str, node_fn):
        """Wrap a node with EvolutionWrapper if evolution is enabled."""
        if not self._config.get("evolution_enabled", False):
            return node_fn
        try:
            _ensure_evolution_imports()
            base_dir = Path(self._config["evolution_base_dir"])
            # Share one Chroma client across all agents to avoid loading the
            # ONNX embedding model 15 times.
            client = _get_shared_chroma_client(base_dir)
            memory = AgentMemorySystem(
                role,
                base_dir=base_dir,
                strategies_dir=Path(self._config["custom_strategies_dir"]),
                top_k=self._config.get("evolution_top_k_episodes", 3),
                chroma_client=client,
            )
            return EvolutionWrapper(role, node_fn, memory)
        except Exception as e:
            # If evolution setup fails (e.g. chromadb not installed), fall back
            # to the bare node — but say so: silently losing the evolution layer
            # is indistinguishable from "it worked" in the logs (B11).
            logger.warning(
                "evolution wrapper unavailable for role %r, running bare node: %s",
                role,
                e,
            )
            return node_fn

    def setup_graph(
        self, selected_analysts=["market", "social", "news", "fundamentals", "policy", "hot_money"]
    ):
        """Set up and compile the agent workflow graph.

        Args:
            selected_analysts (list): List of analyst types to include. Options are:
                - "market": Market analyst (technical analysis)
                - "social": Social media / sentiment analyst
                - "news": News analyst
                - "fundamentals": Fundamentals analyst
                - "policy": Policy analyst (A-stock specific)
                - "hot_money": Hot money / capital flow tracker (A-stock specific)
        """
        if len(selected_analysts) == 0:
            raise ValueError("Trading Agents Graph Setup Error: no analysts selected!")

        # Create analyst nodes
        analyst_nodes = {}
        delete_nodes = {}
        tool_nodes = {}

        if "market" in selected_analysts:
            analyst_nodes["market"] = self._wrap_evolution("market", create_market_analyst(self.llm_for("market")))
            delete_nodes["market"] = create_msg_delete()
            tool_nodes["market"] = self.tool_nodes["market"]

        if "social" in selected_analysts:
            analyst_nodes["social"] = self._wrap_evolution("social", create_social_media_analyst(self.llm_for("social")))
            delete_nodes["social"] = create_msg_delete()
            tool_nodes["social"] = self.tool_nodes["social"]

        if "news" in selected_analysts:
            analyst_nodes["news"] = self._wrap_evolution("news", create_news_analyst(self.llm_for("news")))
            delete_nodes["news"] = create_msg_delete()
            tool_nodes["news"] = self.tool_nodes["news"]

        if "fundamentals" in selected_analysts:
            analyst_nodes["fundamentals"] = self._wrap_evolution("fundamentals", create_fundamentals_analyst(self.llm_for("fundamentals")))
            delete_nodes["fundamentals"] = create_msg_delete()
            tool_nodes["fundamentals"] = self.tool_nodes["fundamentals"]

        if "policy" in selected_analysts:
            analyst_nodes["policy"] = self._wrap_evolution("policy", create_policy_analyst(self.llm_for("policy")))
            delete_nodes["policy"] = create_msg_delete()
            tool_nodes["policy"] = self.tool_nodes["policy"]

        if "hot_money" in selected_analysts:
            analyst_nodes["hot_money"] = self._wrap_evolution("hot_money", create_hot_money_tracker(self.llm_for("hot_money")))
            delete_nodes["hot_money"] = create_msg_delete()
            tool_nodes["hot_money"] = self.tool_nodes["hot_money"]

        # Create quality gate node（B1：感知本次选中的分析师，动态阈值/提示词）
        quality_gate_node = self._wrap_evolution(
            "quality_gate",
            create_quality_gate(self.llm_for("quality_gate"), selected_analysts),
        )

        # Create researcher and manager nodes
        bull_researcher_node = self._wrap_evolution("bull", create_bull_researcher(self.llm_for("bull")))
        bear_researcher_node = self._wrap_evolution("bear", create_bear_researcher(self.llm_for("bear")))
        research_manager_node = self._wrap_evolution("research_manager", create_research_manager(self.llm_for("research_manager")))
        trader_node = self._wrap_evolution("trader", create_trader(self.llm_for("trader")))

        # Create risk analysis nodes
        aggressive_analyst = self._wrap_evolution("risk_aggressive", create_aggressive_debator(self.llm_for("risk_aggressive")))
        neutral_analyst = self._wrap_evolution("risk_neutral", create_neutral_debator(self.llm_for("risk_neutral")))
        conservative_analyst = self._wrap_evolution("risk_conservative", create_conservative_debator(self.llm_for("risk_conservative")))
        portfolio_manager_node = self._wrap_evolution("portfolio_manager", create_portfolio_manager(self.llm_for("portfolio_manager")))

        # Create workflow
        workflow = StateGraph(AgentState)

        # O1: analysts run in parallel by default. Each analyst gets a
        # private message channel ({role}_messages) so concurrent tool loops
        # can't pollute each other; the Quality Gate acts as the fan-in
        # barrier and only fires after every analyst chain finishes.
        parallel = bool(self._config.get("parallel_analysts", True))

        # Add analyst nodes to the graph
        if parallel:
            # 分析师并行层封装为子图（"Analysts Stage"）：子图的 END 是
            # LangGraph 真正的 fan-in barrier，主图要等全部分析师链完成后
            # 才会进入 Quality Gate。
            # 此前直接把各链的条件边指向 "Quality Gate"——条件边汇入的节点
            # 是「任一分支到达即执行」而非等待全部分支：各分析师的工具循环
            # 轮数天然不同，先完成的链就会触发一次 Quality Gate 并把流程
            # 推进到辩论阶段，导致 (a) 质量门控在多数报告仍为空时就输出
            # F 级摘要，(b) 辩论与分析循环并行、多个辩手在同一 superstep
            # 写 investment_debate_state（曾触发 INVALID_CONCURRENT_GRAPH_
            # UPDATE 崩溃，merge reducer 只是掩盖了症状）。
            analyst_stage = StateGraph(AgentState)
            for analyst_type, node in analyst_nodes.items():
                channel = ANALYST_CHANNEL_KEYS[analyst_type]
                analyst_stage.add_node(
                    f"{analyst_type.capitalize()} Analyst",
                    _channel_adapter(channel, node),
                )
                analyst_stage.add_node(
                    f"tools_{analyst_type}",
                    _tool_channel_adapter(channel, tool_nodes[analyst_type]),
                )
                analyst_stage.add_edge(START, f"{analyst_type.capitalize()} Analyst")
                analyst_stage.add_conditional_edges(
                    f"{analyst_type.capitalize()} Analyst",
                    _channel_route(
                        channel, f"tools_{analyst_type}", END
                    ),
                    [f"tools_{analyst_type}", END],
                )
                analyst_stage.add_edge(
                    f"tools_{analyst_type}", f"{analyst_type.capitalize()} Analyst"
                )
            workflow.add_node("Analysts Stage", analyst_stage.compile())
        else:
            for analyst_type, node in analyst_nodes.items():
                workflow.add_node(f"{analyst_type.capitalize()} Analyst", node)
                workflow.add_node(
                    f"Msg Clear {analyst_type.capitalize()}", delete_nodes[analyst_type]
                )
                workflow.add_node(f"tools_{analyst_type}", tool_nodes[analyst_type])

        # Add quality gate + other nodes
        workflow.add_node("Quality Gate", quality_gate_node)
        workflow.add_node("Bull Researcher", bull_researcher_node)
        workflow.add_node("Bear Researcher", bear_researcher_node)
        workflow.add_node("Research Manager", research_manager_node)
        workflow.add_node("Trader", trader_node)
        workflow.add_node("Aggressive Analyst", aggressive_analyst)
        workflow.add_node("Neutral Analyst", neutral_analyst)
        workflow.add_node("Conservative Analyst", conservative_analyst)
        workflow.add_node("Portfolio Manager", portfolio_manager_node)

        # Define edges
        if parallel:
            # 主图：分析师阶段（子图，内部 6 链并行工具循环）作为单个节点
            # 串行接入 Quality Gate —— 阶段内并行、阶段间串行。
            workflow.add_edge(START, "Analysts Stage")
            workflow.add_edge("Analysts Stage", "Quality Gate")
        else:
            # Sequential chain (original behaviour, config parallel_analysts=False)
            # Start with the first analyst
            first_analyst = selected_analysts[0]
            workflow.add_edge(START, f"{first_analyst.capitalize()} Analyst")

            # Connect analysts in sequence
            for i, analyst_type in enumerate(selected_analysts):
                current_analyst = f"{analyst_type.capitalize()} Analyst"
                current_tools = f"tools_{analyst_type}"
                current_clear = f"Msg Clear {analyst_type.capitalize()}"

                # Add conditional edges for current analyst
                workflow.add_conditional_edges(
                    current_analyst,
                    getattr(self.conditional_logic, f"should_continue_{analyst_type}"),
                    [current_tools, current_clear],
                )
                workflow.add_edge(current_tools, current_analyst)

                # Connect to next analyst or to Bull Researcher if this is the last analyst
                if i < len(selected_analysts) - 1:
                    next_analyst = f"{selected_analysts[i+1].capitalize()} Analyst"
                    workflow.add_edge(current_clear, next_analyst)
                else:
                    workflow.add_edge(current_clear, "Quality Gate")

        workflow.add_edge("Quality Gate", "Bull Researcher")

        # Add remaining edges
        workflow.add_conditional_edges(
            "Bull Researcher",
            self.conditional_logic.should_continue_debate,
            {
                "Bear Researcher": "Bear Researcher",
                "Research Manager": "Research Manager",
            },
        )
        workflow.add_conditional_edges(
            "Bear Researcher",
            self.conditional_logic.should_continue_debate,
            {
                "Bull Researcher": "Bull Researcher",
                "Research Manager": "Research Manager",
            },
        )
        workflow.add_edge("Research Manager", "Trader")
        workflow.add_edge("Trader", "Aggressive Analyst")
        workflow.add_conditional_edges(
            "Aggressive Analyst",
            self.conditional_logic.should_continue_risk_analysis,
            {
                "Conservative Analyst": "Conservative Analyst",
                "Portfolio Manager": "Portfolio Manager",
            },
        )
        workflow.add_conditional_edges(
            "Conservative Analyst",
            self.conditional_logic.should_continue_risk_analysis,
            {
                "Neutral Analyst": "Neutral Analyst",
                "Portfolio Manager": "Portfolio Manager",
            },
        )
        workflow.add_conditional_edges(
            "Neutral Analyst",
            self.conditional_logic.should_continue_risk_analysis,
            {
                "Aggressive Analyst": "Aggressive Analyst",
                "Portfolio Manager": "Portfolio Manager",
            },
        )

        workflow.add_edge("Portfolio Manager", END)

        return workflow
