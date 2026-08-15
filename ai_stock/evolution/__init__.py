"""Self-evolution framework for AI Stock agents.

Provides episodic memory (Chroma), custom strategy loading, and a three-layer
memory system (semantic / episodic / working) that can be transparently
wrapped around any agent node via EvolutionWrapper.

Modules:
    vector_store      – Chroma-backed episodic vector store
    experience_store  – JSON + Chroma dual-write experience recorder
    strategy_loader   – Loads custom strategy .md files from disk
    memory_system     – Three-layer memory (semantic / episodic / working)
    agent_wrapper     – EvolutionWrapper: transparent node wrapper
    review_engine     – Periodic review of episodes → learnings
    local_evolver     – Strategy modification draft generator (human-approved)
    scheduler         – APScheduler-based periodic review scheduler
    market_regime     – CSI 300 market regime classifier
    weight_allocator  – Regime-dependent agent weight assignment
    global_coordinator – Cross-agent conflict detection + global report
"""

from .vector_store import EpisodicVectorStore
from .experience_store import ExperienceStore
from .strategy_loader import StrategyLoader
from .memory_system import AgentMemorySystem
from .agent_wrapper import EvolutionWrapper
from .review_engine import ReviewEngine
from .local_evolver import LocalEvolver
from .scheduler import EvolutionScheduler
from .market_regime import MarketRegimeDetector
from .weight_allocator import WeightAllocator
from .global_coordinator import GlobalCoordinator

__all__ = [
    "EpisodicVectorStore",
    "ExperienceStore",
    "StrategyLoader",
    "AgentMemorySystem",
    "EvolutionWrapper",
    "ReviewEngine",
    "LocalEvolver",
    "EvolutionScheduler",
    "MarketRegimeDetector",
    "WeightAllocator",
    "GlobalCoordinator",
]
