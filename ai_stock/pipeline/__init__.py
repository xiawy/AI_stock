"""榜单层 (新闻榜/行业榜/热股榜) 的存储、读取与备份.

- **新闻榜** — 直接取 quant 选股流程第一步 (MacroEventAgent) 抓取的新闻前 20 条,
  政策优先 + 时间倒序, 落库到 ImpactSnapshot/NewsItem (见 news_board.py)。
  重型多 Agent 评分/辩论流程已移除。
- **行业榜/热股榜** — quant 选股流程产出 (ai_stock.quant.db_ops)。
- **备份** — 每日 23:30 导出三榜到日期 JSON 文件 (见 backup.py)。
"""
