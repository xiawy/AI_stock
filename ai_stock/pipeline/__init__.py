"""News impact assessment & smart stock recommendation pipeline.

Two core features built on top of the existing agent framework:

1. **News/Policy Impact Assessment** — collect 12h of news, score with 4
   parallel agents, quantify supply-demand gaps, run bull/bear debates,
   and rank the Top 20 most impactful events.

2. **Smart Stock Recommendation** — combine Top 5 bullish events with
   recent limit-up stocks, score candidates on fundamentals / technicals
   / event-matching, run per-stock debates, and output Top 10 buys.

Scheduled at 08:00 and 20:00 daily via APScheduler.
"""
