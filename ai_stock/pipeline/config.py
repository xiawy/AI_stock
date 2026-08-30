"""Static configuration for the ranking layer.

These complement the main ``DEFAULT_CONFIG`` in ``ai_stock/default_config.py``.
重型新闻影响力评估流程 (多 Agent 评分/辩论) 已移除; 新闻榜直接取选股流程
采集的新闻前 20 条 (见 ai_stock.pipeline.news_board), 配置只余榜单条数与
备份时点。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# News board (新闻榜)
# ---------------------------------------------------------------------------

# Number of items in the news board ranking
TOP_N_IMPACT = 20

# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

# Daily ranking backup slot (local time). After this point the day's
# rankings (新闻榜 + quant 行业榜/热股榜) are exported to a dated JSON file;
# see ai_stock.pipeline.backup. Keep it before the 03:30 cleanup pass.
BACKUP_DAILY_AT = (23, 30)
