"""Research Manager: turns the bull/bear debate into a structured investment plan for the trader."""

from __future__ import annotations

from ai_stock.agents.schemas import ResearchPlan, render_research_plan
from ai_stock.agents.utils.agent_utils import build_instrument_context, get_language_instruction
from ai_stock.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)


def create_research_manager(llm):
    structured_llm = bind_structured(llm, ResearchPlan, "Research Manager")

    def research_manager_node(state) -> dict:
        instrument_context = build_instrument_context(state["company_of_interest"])
        history = state["investment_debate_state"].get("history", "")

        investment_debate_state = state["investment_debate_state"]

        # 数据依赖（阶段三）：辩论记录 + 6 份研报（通过 State）+ 质量门控摘要。
        # 辩手的论证是对研报的提炼，最终裁决必须能回到原始证据核对，
        # 防止辩论中的夸大/遗漏直接变成投资计划。
        market_research_report = state.get("market_report", "")
        sentiment_report = state.get("sentiment_report", "")
        news_report = state.get("news_report", "")
        fundamentals_report = state.get("fundamentals_report", "")
        policy_report = state.get("policy_report", "")
        hot_money_report = state.get("hot_money_report", "")
        data_quality_summary = state.get("data_quality_summary", "")

        prompt = f"""As the Research Manager and debate facilitator, your role is to critically evaluate this round of debate and deliver a clear, actionable investment plan for the trader.

{instrument_context}

Note: This is an A-share (China mainland) stock. Factor in regulatory policy impact and hot money / capital flow dynamics when synthesising the debate.

---
**INVESTMENT DECISION CORE FRAMEWORK** (apply these principles to your analysis):

1. **Strategic Direction (主线与节奏)**:
   - Always align with the national strategic objectives (e.g., "breakthrough in bottleneck technologies", "AI+industrial upgrading") – these are the most durable market drivers.
   - Identify the primary theme and assess its stage: is it in early breakout, accelerated main uptrend, or late-stage overheating? Timing mistakes (e.g., chasing at peaks, panic-selling at corrections) can be more damaging than wrong strategy.

2. **Positioning Horizon (长线与短线)**:
   - For core holdings in the main theme, adopt a "long-term base + tactical roll" approach: hold a substantial base position through fluctuations, and use a small portion (20-30%) for tactical trading based on sentiment/technical signals.
   - Avoid excessive short-term trading, especially in the current quant-and-hot-money dominated market – it is a disadvantaged game for individual investors.

3. **Concentration vs. Diversification (集中与分散)**:
   - Favor focused concentration (2-3 sectors, 3-5 core stocks) over over-diversification. Deep research on a few names beats superficial coverage of many.
   - Adjust positions dynamically as sub-sectors rotate within the main theme.

4. **Patience vs. Decisiveness (耐心与果断)**:
   - Be patient in waiting for entry (pullback to support), holding through consolidation, and exiting after a strong run.
   - Be decisive when conditions change:
     * **Stop-Loss**: if price breaks key support (e.g., -5%~-8%) or shows "false breakout", exit immediately to preserve capital.
     * **Stop-Being-Wrong (止错)**: if the underlying industry logic collapses (e.g., technology substitution, worsening competition, policy shift), exit regardless of profit/loss – time is no longer on your side.

5. **Market Phase Recognition (反弹 vs. 反转, 筑底 vs. 主升)**:
   - Distinguish between a tactical bounce (lack of sector synergy, sporadic volume, irregular price action, driven by oversold or minor news) and a true trend reversal (strong sector-wide move, sustained volume, clear leadership, backed by industry fundamentals).
   - Only commit larger, longer-term positions when you identify a confirmed reversal with solid fundamentals; otherwise treat it as a short-term swing.

---
**Rating Scale** (use exactly one):
- **Buy**: Strong conviction in the bull thesis; recommend taking or growing the position
- **Overweight**: Constructive view; recommend gradually increasing exposure
- **Hold**: Balanced view; recommend maintaining the current position
- **Underweight**: Cautious view; recommend trimming exposure
- **Sell**: Strong conviction in the bear thesis; recommend exiting or avoiding the position

Commit to a clear stance whenever the debate's strongest arguments warrant one; reserve Hold for situations where the evidence on both sides is genuinely balanced.

---

**Analyst Research Reports** (primary evidence):
Market research report: {market_research_report}
Social media sentiment report: {sentiment_report}
Latest news report: {news_report}
Company fundamentals report: {fundamentals_report}
Policy analysis report: {policy_report}
Hot money / capital flow report: {hot_money_report}

**Data Quality Assessment:** {data_quality_summary}

---

**Debate History:**
{history}

---

Weigh the debate arguments against the underlying reports: when a debater overstates or ignores evidence in the reports, trust the reports. Reduce the weight of any report the data quality assessment flags as low-confidence (grade C/D/F) and note the limitation in your plan.

In your final investment plan, explicitly address:
- Which market phase you believe the stock/sector is in (using the above framework).
- Your strategic stance (long-term base vs. tactical play) and the corresponding position sizing.
- Key risk points and the specific conditions under which you would trigger stop-loss or stop-being-wrong. 

""" + get_language_instruction()

        # Inject evolution context if available
        evo_ctx = state.get("evolution_context")
        if evo_ctx:
            prompt += f"\n\n---\n## 自进化上下文\n{evo_ctx}\n---"

        investment_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_research_plan,
            "Research Manager",
        )

        new_investment_debate_state = {
            "judge_decision": investment_plan,
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": investment_plan,
            "count": investment_debate_state["count"],
        }

        return {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": investment_plan,
        }

    return research_manager_node
