"""Pipeline evolution integration — self-memory + custom strategies for pipeline LLM calls.

The main LangGraph agents (Analyst, Bull/Bear, Trader, etc.) use EvolutionWrapper
from ai_stock.evolution to inject custom strategies and past episodes into prompts.

This module provides the SAME capability for the pipeline modules (news impact
assessment + stock recommendation), which call LLMs directly without LangGraph.

Key components:
- ``init_evolution(config)``: Initialize evolution context for a pipeline run.
- ``get_evolution_context()``: Get the current evolution context string.
- ``wrap_llm(llm, agent_name)``: Wrap an LLM to auto-inject context + record episodes.

Usage in pipeline modules::

    from .evolution import get_evolution_context, wrap_llm

    # In a function that calls LLM:
    ctx = get_evolution_context()
    if ctx:
        prompt += f"\\n\\n{ctx}"

    # Or wrap the LLM for transparent injection:
    wrapped = wrap_llm(llm, "policy")
    result = wrapped.invoke(prompt)  # context auto-injected, episode auto-recorded
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state (set by init_evolution, read by get_evolution_context)
# ---------------------------------------------------------------------------

_evolution_context: str = ""
_evolution_enabled: bool = False
_chroma_client = None  # Shared across all agents to avoid loading ONNX model N times
_memory_cache: dict[str, Any] = {}  # agent_name → AgentMemorySystem


def init_evolution(config: dict) -> str:
    """Initialize the evolution system for a pipeline run.

    Reads config, builds evolution context from all pipeline agent memories,
    and stores it module-locally. Returns the context string (empty if disabled).

    Safe to call even when evolution is disabled or chromadb is not installed.
    """
    global _evolution_context, _evolution_enabled, _chroma_client, _memory_cache

    _evolution_context = ""
    _evolution_enabled = False
    _memory_cache = {}

    if not config.get("evolution_enabled", False):
        logger.debug("Evolution disabled in config")
        return ""

    try:
        from ai_stock.evolution.memory_system import AgentMemorySystem
    except ImportError:
        logger.debug("Evolution system not available (chromadb not installed?)")
        return ""

    base_dir = Path(config.get("evolution_base_dir", "~/.tradingagents/evolution_data")).expanduser()
    strategies_dir = Path(config.get("custom_strategies_dir", "custom_strategies"))
    top_k = config.get("evolution_top_k_episodes", 3)

    # Shared Chroma client to avoid loading ONNX embedding model multiple times
    try:
        import chromadb
        _chroma_client = chromadb.PersistentClient(path=str(base_dir))
    except Exception:
        _chroma_client = None

    # Pipeline agent roles that benefit from evolution
    pipeline_roles = [
        "pipeline_policy", "pipeline_news", "pipeline_capital", "pipeline_sentiment",
        "pipeline_supply_demand",
        "pipeline_bull", "pipeline_bear", "pipeline_research_manager",
        "pipeline_fundamentals", "pipeline_technical", "pipeline_event_match",
        "pipeline_stock_bull", "pipeline_stock_bear", "pipeline_stock_judge",
        "pipeline_recommendation",
        "pipeline_candidate_pool",
    ]

    # Build memory systems for each role
    curr_date = datetime.now().strftime("%Y-%m-%d")
    context_parts = []

    for role in pipeline_roles:
        try:
            memory = AgentMemorySystem(
                role,
                base_dir=base_dir,
                strategies_dir=strategies_dir,
                top_k=top_k,
                chroma_client=_chroma_client,
            )
            _memory_cache[role] = memory

            # Build context: custom strategies + similar past episodes
            ctx = memory.build_evolution_context(ticker="pipeline", trade_date=curr_date)
            if ctx:
                context_parts.append(f"### {role} 策略与经验\n{ctx}")
        except Exception:
            logger.debug("Failed to build evolution context for %s", role, exc_info=True)

    _evolution_context = "\n\n".join(context_parts)
    _evolution_enabled = True

    if _evolution_context:
        logger.info(
            "Evolution context built: %d chars from %d roles",
            len(_evolution_context), len(context_parts),
        )
    else:
        logger.debug("No evolution context generated")

    return _evolution_context


def get_evolution_context() -> str:
    """Get the current evolution context string. Empty if not initialized or disabled."""
    return _evolution_context


def is_evolution_enabled() -> bool:
    """Check if evolution is enabled for the current pipeline run."""
    return _evolution_enabled


def get_memory(agent_name: str) -> Optional[Any]:
    """Get the AgentMemorySystem for a specific agent, or None if not available."""
    return _memory_cache.get(agent_name)


def record_episode(agent_name: str, input_summary: str, output_summary: str) -> None:
    """Record an episode for a pipeline agent.

    Args:
        agent_name: The agent role name (e.g., "pipeline_policy").
        input_summary: Brief description of the input.
        output_summary: Brief description of the output.
    """
    memory = _memory_cache.get(agent_name)
    if memory is None:
        return

    try:
        memory.episodic.record({
            "ticker": "pipeline",
            "date": datetime.now().strftime("%Y-%m-%d"),
            "agent": agent_name,
            "input_summary": input_summary[:500],
            "output_summary": output_summary[:500],
            "outcome": "pending",
        })
    except Exception:
        logger.debug("Failed to record episode for %s", agent_name, exc_info=True)


# ---------------------------------------------------------------------------
# LLM wrapper — transparent context injection + episode recording
# ---------------------------------------------------------------------------


class _StructuredLlmWrapper:
    """Wraps a structured LLM (from with_structured_output) to inject context + record."""

    def __init__(self, structured_llm: Any, agent_name: str, context: str):
        self._structured_llm = structured_llm
        self._agent_name = agent_name
        self._context = context

    def invoke(self, prompt: str, *args: Any, **kwargs: Any) -> Any:
        if self._context:
            prompt = f"{prompt}\n\n{self._context}"

        result = self._structured_llm.invoke(prompt, *args, **kwargs)

        # Record episode
        try:
            if hasattr(result, "model_dump_json"):
                output = result.model_dump_json()[:500]
            else:
                output = str(result)[:500]
            record_episode(self._agent_name, prompt[:500], output)
        except Exception:
            pass

        return result


class EvolutionLlm:
    """Wraps an LLM to transparently inject evolution context and record episodes.

    Usage::

        wrapped = EvolutionLlm(llm, "pipeline_policy")
        result = wrapped.invoke(prompt)          # context auto-injected
        structured = wrapped.with_structured_output(Schema)
        result = structured.invoke(prompt)       # context auto-injected
    """

    def __init__(self, llm: Any, agent_name: str, context: str | None = None):
        self._llm = llm
        self._agent_name = agent_name
        self._context = context if context is not None else get_evolution_context()

    def invoke(self, prompt: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke the LLM with evolution context appended to the prompt."""
        if self._context:
            prompt = f"{prompt}\n\n{self._context}"

        result = self._llm.invoke(prompt, *args, **kwargs)

        # Record episode
        try:
            if hasattr(result, "content"):
                output = result.content[:500] if isinstance(result.content, str) else str(result)[:500]
            else:
                output = str(result)[:500]
            record_episode(self._agent_name, prompt[:500], output)
        except Exception:
            pass

        return result

    def with_structured_output(self, schema: Any, *args: Any, **kwargs: Any) -> _StructuredLlmWrapper:
        """Return a wrapped structured LLM that also injects context + records."""
        structured_llm = self._llm.with_structured_output(schema, *args, **kwargs)
        return _StructuredLlmWrapper(structured_llm, self._agent_name, self._context)

    def __getattr__(self, name: str) -> Any:
        """Forward all other attributes/methods to the underlying LLM."""
        return getattr(self._llm, name)


def wrap_llm(llm: Any, agent_name: str) -> EvolutionLlm:
    """Convenience function to wrap an LLM with evolution capabilities.

    Args:
        llm: The LLM instance to wrap.
        agent_name: The agent role name for memory lookup (e.g., "pipeline_policy").

    Returns:
        An EvolutionLlm that transparently injects context and records episodes.
    """
    return EvolutionLlm(llm, agent_name)
