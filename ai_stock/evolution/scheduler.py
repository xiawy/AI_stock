
"""APScheduler-based scheduler for periodic evolution reviews.

Runs review + draft generation on a configurable schedule (default: Tue/Thu/Sun
post-market at 16:00 CST).  Also supports a volatility trigger that auto-runs
a review when market volatility spikes.

Note: In CLI mode the scheduler exits with the process.  It only runs
persistently when the backend server is running.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class EvolutionScheduler:
    """APScheduler-based scheduler for periodic reviews."""

    def __init__(
        self,
        agents: List[str],
        config: Dict[str, Any],
        review_fn=None,
        volatility_fn=None,
    ) -> None:
        """
        Args:
            agents: List of agent names to review.
            config: Global config dict (reads review_schedule, etc.).
            review_fn: Callable(agent_name) -> dict. Runs the review for one agent.
            volatility_fn: Callable() -> bool. Returns True if volatility spike detected.
        """
        self.agents = agents
        self.config = config
        self._review_fn = review_fn
        self._volatility_fn = volatility_fn
        self._scheduler = None

    def start(self) -> None:
        """Start the scheduler."""
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.weekly import Weekly
        except ImportError:
            logger.warning(
                "APScheduler not installed. Evolution scheduler disabled. "
                "Install with: pip install 'apscheduler>=3.10'"
            )
            return

        self._scheduler = BackgroundScheduler()

        # Schedule periodic reviews (default: Tue/Thu/Sun at 16:00)
        schedule = self.config.get("review_schedule", ["Tue", "Thu", "Sun"])
        # APScheduler Weekly trigger expects lowercase day abbreviations
        day_map = {"Mon": "mon", "Tue": "tue", "Wed": "wed", "Thu": "thu",
                   "Fri": "fri", "Sat": "sat", "Sun": "sun"}
        for day in schedule:
            apscheduler_day = day_map.get(day, day.lower())
            self._scheduler.add_job(
                self._run_all_reviews,
                Weekly(day=apscheduler_day, hour=16, minute=0),
                id="evolution_review",
                name="Evolution periodic review",
                replace_existing=True,
            )

        # Optional volatility trigger (daily at 15:30)
        if self.config.get("review_volatility_trigger", False):
            self._scheduler.add_job(
                self._check_volatility_trigger,
                Weekly(day="mon,tue,wed,thu,fri", hour=15, minute=30),
                id="volatility_trigger",
                name="Volatility trigger check",
                replace_existing=True,
            )

        self._scheduler.start()
        logger.info(
            "Evolution scheduler started: reviews on %s at 16:00, volatility trigger=%s",
            ", ".join(schedule),
            self.config.get("review_volatility_trigger", False),
        )

    def stop(self) -> None:
        """Stop the scheduler."""
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            logger.info("Evolution scheduler stopped")

    def _run_all_reviews(self) -> None:
        """Run review for all configured agents."""
        if not self._review_fn:
            return
        for agent_name in self.agents:
            try:
                self._review_fn(agent_name)
            except Exception:
                logger.warning("Review failed for agent '%s'", agent_name, exc_info=True)

    def _check_volatility_trigger(self) -> None:
        """Check if volatility spike triggers an unscheduled review."""
        if not self._volatility_fn or not self._review_fn:
            return
        try:
            if self._volatility_fn():
                logger.info("Volatility spike detected — triggering unscheduled review")
                self._run_all_reviews()
        except Exception:
            logger.warning("Volatility trigger check failed", exc_info=True)
