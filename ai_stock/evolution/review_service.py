"""Human-in-the-loop review service for the evolution system.

Shared by the CLI (``cli/main.py``) and the web backend
(``backend/app/services/evolution_service.py``). Everything in this module is
synchronous and depends only on the ``ai_stock`` packages — the backend layer
adds background job tracking on top.

Review flow (nothing is ever auto-applied):
1. ``run_review_for_agent`` reads the agent's JSON episodes, asks the LLM for a
   review summary + improvement suggestions (:class:`ReviewEngine`), then uses
   the current custom strategy to generate an updated strategy draft
   (:class:`LocalEvolver`).
2. Drafts land in ``{evolution_base_dir}/review_queue/{agent}/draft_*.md``.
3. A human approves/rejects them via the CLI (``ai-stock draft-review``) or the
   web 进化审核 page — ``approve_draft`` is the only code path that modifies
   the active strategy files (after backing them up).
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# Canonical agent set — mirrors the custom_strategies/ directories and the
# CLI's `evolve` list.
AGENTS: List[str] = [
    "market", "fundamentals", "hot_money", "policy", "social", "news",
    "bull", "bear", "trader", "risk", "quality_gate", "portfolio",
]

_REVIEW_QUEUE_DIRNAME = "review_queue"


# ---------------------------------------------------------------------------
# Config / validation helpers
# ---------------------------------------------------------------------------


def build_llm(config: dict) -> Any:
    """Build an LLM for review / draft generation from the given config.

    Uses the same factory as the pipeline service so the review model follows
    ``llm_provider`` / ``deep_think_llm`` / ``backend_url`` conventions.
    """
    from ai_stock.llm_clients.factory import create_llm_client

    provider = config.get("llm_provider", "openai")
    model = (
        config.get("deep_think_llm")
        or config.get("quick_think_llm")
        or "gpt-5.4-mini"
    )
    kwargs = {}
    if config.get("max_tokens"):
        kwargs["max_tokens"] = config["max_tokens"]
    return create_llm_client(
        provider=provider,
        model=model,
        base_url=config.get("backend_url"),
        **kwargs,
    ).get_llm()


def review_queue_dir(config: dict) -> Path:
    """Directory that holds one subdirectory per agent with pending drafts."""
    return Path(config["evolution_base_dir"]) / _REVIEW_QUEUE_DIRNAME


def resolve_agents(agent: str) -> List[str]:
    """Turn the CLI/API ``agent`` selector into a concrete agent list."""
    if agent == "all":
        return list(AGENTS)
    if agent in AGENTS:
        return [agent]
    raise ValueError(f"未知 Agent: {agent}。可用: {', '.join(AGENTS)}")


def validate_agent(agent_name: str, config: dict) -> str:
    """Return the agent name if known (canonical list or a strategy dir)."""
    known = set(AGENTS)
    strategies_dir = Path(config["custom_strategies_dir"])
    if strategies_dir.is_dir():
        known |= {d.name for d in strategies_dir.iterdir() if d.is_dir()}
    if agent_name not in known:
        raise ValueError(f"未知 Agent: {agent_name}。可用: {', '.join(sorted(known))}")
    return agent_name


def validate_draft_filename(filename: str) -> str:
    """Allow only ``draft_*.md`` filenames — blocks path traversal."""
    name = Path(filename).name
    if name != filename or not (name.startswith("draft_") and name.endswith(".md")):
        raise ValueError(f"非法草稿文件名: {filename}")
    return name


# ---------------------------------------------------------------------------
# Review execution
# ---------------------------------------------------------------------------


def run_review_for_agent(
    agent_name: str,
    config: dict,
    llm: Any = None,
    generate_draft: bool = True,
) -> dict:
    """Run a full review for one agent.

    Reads episodes, generates a summary + improvement suggestions
    (ReviewEngine), then — when a current strategy exists — asks the LLM for an
    updated strategy draft (LocalEvolver). The draft is left in the review
    queue; nothing is applied to the active strategy directory.

    Returns a dict with the review text, episode statistics and the generated
    draft metadata (if any).
    """
    from ai_stock.evolution.local_evolver import LocalEvolver
    from ai_stock.evolution.memory_system import AgentMemorySystem
    from ai_stock.evolution.review_engine import ReviewEngine

    validate_agent(agent_name, config)

    base_dir = Path(config["evolution_base_dir"])
    strategies_dir = Path(config["custom_strategies_dir"])
    learnings_dir = Path(config["learnings_dir"])

    memory = AgentMemorySystem(
        agent_name,
        base_dir=base_dir,
        strategies_dir=strategies_dir,
        top_k=config.get("evolution_top_k_episodes", 3),
    )

    if llm is None:
        llm = build_llm(config)

    review = ReviewEngine(agent_name, memory, llm, learnings_dir).run_review()

    draft = None
    current_strategy = memory.semantic.load_all()
    if (
        generate_draft
        and review.get("episodes_total", 0) > 0
        and review.get("suggestions")
        and current_strategy
    ):
        evolver = LocalEvolver(
            agent_name,
            llm,
            strategies_dir=strategies_dir,
            review_queue_dir=review_queue_dir(config),
        )
        draft_content = evolver.generate_draft(current_strategy, review["suggestions"])
        if draft_content:
            draft = {"agent": agent_name, "content": draft_content}

    return {**review, "agent": agent_name, "draft": draft}


# ---------------------------------------------------------------------------
# Draft queue (list / view / approve / reject)
# ---------------------------------------------------------------------------


def list_drafts(config: dict, agent_name: Optional[str] = None) -> List[dict]:
    """List pending drafts in the review queue, newest first."""
    queue_root = review_queue_dir(config)
    if not queue_root.is_dir():
        return []

    drafts: List[dict] = []
    for agent_dir in sorted(queue_root.iterdir()):
        if not agent_dir.is_dir():
            continue
        if agent_name and agent_dir.name != agent_name:
            continue
        for fp in sorted(agent_dir.glob("draft_*.md"), reverse=True):
            drafts.append(_draft_meta(agent_dir.name, fp))
    return drafts


def get_draft(config: dict, agent_name: str, filename: str) -> dict:
    """Return a single draft with its full content."""
    agent_name = validate_agent(agent_name, config)
    filename = validate_draft_filename(filename)
    fp = review_queue_dir(config) / agent_name / filename
    if not fp.exists():
        raise FileNotFoundError(f"草稿不存在: {agent_name}/{filename}")
    meta = _draft_meta(agent_name, fp)
    meta["content"] = fp.read_text(encoding="utf-8")
    return meta


def approve_draft(config: dict, agent_name: str, filename: str) -> dict:
    """Approve a draft and apply it to the active strategy directory.

    ``LocalEvolver.apply_approved`` backs up the current strategies first and
    then replaces them with the draft. The applied draft is removed from the
    review queue so it cannot be applied twice.
    """
    from ai_stock.evolution.local_evolver import LocalEvolver

    agent_name = validate_agent(agent_name, config)
    filename = validate_draft_filename(filename)

    queue_dir = review_queue_dir(config) / agent_name
    fp = queue_dir / filename
    if not fp.exists():
        raise FileNotFoundError(f"草稿不存在: {agent_name}/{filename}")

    evolver = LocalEvolver(
        agent_name,
        llm=None,  # apply_approved only copies files — no LLM needed.
        strategies_dir=Path(config["custom_strategies_dir"]),
        review_queue_dir=review_queue_dir(config),
    )
    evolver.apply_approved(fp)

    # Remove the applied draft from the queue (it is now the active strategy).
    fp.unlink()
    logger.info("Approved draft applied for agent '%s': %s", agent_name, filename)
    return {"agent": agent_name, "filename": filename, "detail": "已批准并应用（原策略已备份）"}


def reject_draft(config: dict, agent_name: str, filename: str) -> dict:
    """Reject a draft — simply delete it from the review queue."""
    agent_name = validate_agent(agent_name, config)
    filename = validate_draft_filename(filename)

    fp = review_queue_dir(config) / agent_name / filename
    if not fp.exists():
        raise FileNotFoundError(f"草稿不存在: {agent_name}/{filename}")
    fp.unlink()
    logger.info("Rejected draft for agent '%s': %s", agent_name, filename)
    return {"agent": agent_name, "filename": filename, "detail": "已拒绝并删除草稿"}


def _draft_meta(agent_name: str, fp: Path) -> dict:
    mtime = datetime.fromtimestamp(fp.stat().st_mtime)
    preview = ""
    try:
        text = fp.read_text(encoding="utf-8")[:200]
        preview = text.replace("\n", " ").strip()
    except OSError:
        pass
    return {
        "agent": agent_name,
        "filename": fp.name,
        "path": str(fp),
        "created_at": mtime.isoformat(timespec="seconds"),
        "size": fp.stat().st_size,
        "preview": preview,
    }


# ---------------------------------------------------------------------------
# Status / learning summaries
# ---------------------------------------------------------------------------


def agent_status(config: dict, agents: Optional[List[str]] = None) -> List[dict]:
    """Per-agent overview: episode counts, strategy files, drafts, last review.

    ``agents`` restricts the report to a subset of the canonical AGENTS list
    (default: all of them).
    """
    from ai_stock.evolution.memory_system import AgentMemorySystem

    base_dir = Path(config["evolution_base_dir"])
    strategies_dir = Path(config["custom_strategies_dir"])
    top_k = config.get("evolution_top_k_episodes", 3)
    queue_root = review_queue_dir(config)
    names = agents or list(AGENTS)

    statuses: List[dict] = []
    for agent in names:
        if agent not in AGENTS:
            continue
        try:
            memory = AgentMemorySystem(
                agent,
                base_dir=base_dir,
                strategies_dir=strategies_dir,
                top_k=top_k,
            )
            episodes = memory.episodic.load_all()
            strategy_files = memory.semantic.list_files()
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("Status read failed for '%s': %s", agent, exc)
            memory = None
            episodes = []
            strategy_files = []

        agent_queue = queue_root / agent
        drafts = (
            [p.name for p in agent_queue.glob("draft_*.md")]
            if agent_queue.is_dir()
            else []
        )

        statuses.append(
            {
                "agent": agent,
                "episodes_total": len(episodes),
                "episodes_resolved": sum(
                    1 for e in episodes if e.get("outcome") != "pending"
                ),
                "episodes_pending": sum(
                    1 for e in episodes if e.get("outcome") == "pending"
                ),
                "strategy_files": strategy_files,
                "draft_count": len(drafts),
                "last_learning": latest_learning(config, agent),
            }
        )
    return statuses


def latest_learning(config: dict, agent_name: str) -> Optional[dict]:
    """Return the newest ``{date}_summary.md`` for an agent, if any."""
    learnings_dir = Path(config["learnings_dir"]) / agent_name
    if not learnings_dir.is_dir():
        return None
    summaries = sorted(learnings_dir.glob("*_summary.md"), reverse=True)
    if not summaries:
        return None
    fp = summaries[0]
    try:
        content = fp.read_text(encoding="utf-8")
    except OSError:
        content = ""
    return {
        "path": str(fp),
        "date": fp.stem.replace("_summary", ""),
        "content": content[:4000],
    }
