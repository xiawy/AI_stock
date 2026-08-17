"""Pipeline-specific configuration constants.

These complement the main ``DEFAULT_CONFIG`` in ``ai_stock/default_config.py``.
The pipeline reads them at startup; no runtime overrides are needed.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# News impact assessment
# ---------------------------------------------------------------------------

# Scoring weights for the 4 agents
AGENT_WEIGHTS = {
    "policy": 0.30,
    "news": 0.25,
    "capital": 0.25,
    "sentiment": 0.20,
}

# Minimum composite score to enter the candidate pool
MIN_COMPOSITE_SCORE = 6.0

# Number of items in the final impact ranking
TOP_N_IMPACT = 20

# Max debate rounds per news item
NEWS_DEBATE_MAX_ROUNDS = 2

# ---------------------------------------------------------------------------
# Stock recommendation
# ---------------------------------------------------------------------------

# Number of top bullish events used for candidate pool generation
TOP_N_EVENTS_FOR_CANDIDATES = 5

# Candidate pool size limits
MIN_CANDIDATES = 30
MAX_CANDIDATES = 50

# Scoring weights for the initial composite score
FUNDAMENTALS_WEIGHT = 0.35
TECHNICAL_WEIGHT = 0.35
EVENT_MATCH_WEIGHT = 0.30

# Final scoring weights (after debate)
FINAL_FUNDAMENTALS_WEIGHT = 0.35
FINAL_TECHNICAL_WEIGHT = 0.35
FINAL_EVENT_MATCH_WEIGHT = 0.15
FINAL_DEBATE_WEIGHT = 0.15

# Number of stocks in the final recommendation
TOP_N_RECOMMENDED = 10
TOP_N_ALTERNATES = 3

# Max debate rounds per stock
STOCK_DEBATE_MAX_ROUNDS = 2

# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

# Times at which the pipeline runs (local time), "HH:MM" 24h format.
# - 00:00 generates the day's initial rankings (news from the previous
#   12h window via NEWS_WINDOW_HOURS).
# - 08:30 / 12:30 / 14:30 generate a fresh snapshot, which supersedes the
#   earlier one for that date (readers always get the day's latest
#   completed snapshot).
# The first slot of the day is also the boundary before which the previous
# day's ranking is served as "today" (see PipelineService.ensure_today_data).
PIPELINE_SCHEDULE = ["00:00", "08:30", "12:30", "14:30"]

# Daily ranking backup slot (local time). After this point the day's
# rankings (新闻榜/行业榜/热股榜) are exported to a dated JSON file; see
# ai_stock.pipeline.backup. Keep it before the 03:30 cleanup pass.
BACKUP_DAILY_AT = (23, 30)

# News collection window (hours)
NEWS_WINDOW_HOURS = 12

# Limit-up look-back window (trading days)
LIMIT_UP_DAYS = 7

# ---------------------------------------------------------------------------
# Retry & resilience
# ---------------------------------------------------------------------------

# Max retries for API calls with exponential backoff
MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.0  # seconds

# Max consecutive failures before alerting
MAX_CONSECUTIVE_FAILURES = 3

# ---------------------------------------------------------------------------
# LLM cost control
# ---------------------------------------------------------------------------

# Batch size for LLM scoring (news per batch)
SCORING_BATCH_SIZE = 8

# Max concurrent LLM calls for parallel scoring
SCORING_MAX_WORKERS = 4

# Max concurrent LLM calls for debate
DEBATE_MAX_WORKERS = 3
