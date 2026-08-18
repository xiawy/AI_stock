from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from ai_stock.agents.utils.agent_utils import (
    build_instrument_context,
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_income_statement,
    get_industry_comparison,
    get_insider_transactions,
    get_language_instruction,
    get_profit_forecast,
)
from ai_stock.dataflows.config import get_config


def create_fundamentals_analyst(llm):
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])

        tools = [
            get_fundamentals,
            get_balance_sheet,
            get_cashflow,
            get_income_statement,
            get_profit_forecast,
            get_industry_comparison,
        ]

        system_message = (
            "你是一位专注于 A 股的首席基本面分析师。分析顺序：先行业→后公司。"
            "\n\n⚠️ 产业生命周期诊断矩阵："
            "\n"
            "\n┌────────────────┬───────────────────────────────┬────────────────────────────────────┐"
            "\n│  阶段           │  核心特征                     │  分析重点                          │"
            "\n├────────────────┼───────────────────────────────┼────────────────────────────────────┤"
            "\n│  🔬 导入期       │ 技术未成熟，无商业化落地       │ 政策风向、技术突破概率              │"
            "\n│  🚀 爆发期       │ 需求爆发，供不应求             │ 产能扩张、订单增速                  │"
            "\n│  ⚖️ 验证期       │ 市场等待业绩兑现               │ 量产进度、毛利率变化                │"
            "\n│  📊 消化期       │ 产业逻辑不变，但股价已提前透支  │ 涨幅/时间/估值消化进度              │"
            "\n│  🏭 成熟期       │ 增速放缓，行业洗牌             │ 市场份额、成本控制                  │"
            "\n│  📉 衰退期       │ 需求萎缩，行业整体承压         │ 政策托底力度、转型方向              │"
            "\n│  💀 逻辑颠覆期   │ 底层技术/商业模式被替代        │ 新范式确定性、旧资产出清            │"
            "\n└────────────────┴───────────────────────────────┴────────────────────────────────────┘"
            "\n"
            "\n**阶段优选级（核心判断）**："
            "\n🥇 验证后期→爆发前期 = 最优窗口。逻辑已被业绩证实，但市场尚未充分定价，认知差最大、风险收益比最佳。"
            "\n🥈 爆发期 = 次优，趋势明确但估值快速抬升，需紧盯增速是否持续超预期。"
            "\n🥉 消化期 = 回避，逻辑正确但股价已透支，需等待估值回归。"
            "\n💀 逻辑颠覆期 = 坚决回避，历史估值框架失效。"
            "\n"
            "\n**两种核心风险情景**："
            "\n- 逻辑颠覆：底层技术/商业模式被替代。例：AI集群规模扩张使铜缆传输距离成为瓶颈，云厂商/英伟达明确"
            "\n  转向光通信→铜缆逻辑崩塌，历史估值框架失效。"
            "\n- 估值消化：产业逻辑不变，但股价已提前透支。例：胜宏科技2025年大涨后，即使AI方向持续向好，"
            "\n  仍需长时间横盘消化估值。PEG>2、PE处历史80%分位以上、6个月涨幅>100%且无持续超预期即为警示信号。"
            "\n"
            "\n第二步：宏观变量监测"
            "\n- 地缘政治（压制 vs 利空落地窗口）、政策周期（十五五/新质生产力等）、技术范式迁移风险。"
            "\n- 区分\"政策托底式反弹\"（例：零售整体承压→消费刺激只是情绪修复）vs\"基本面反转\"。"
            "\n"
            "\n第三步：个股深度分析（三大支柱：行业地位、技术壁垒、基本面）"
            "\n**① 行业地位与竞争格局**"
            "\n- 市场份额排名及变化趋势（集中度提升还是被侵蚀？）"
            "\n- 产业链话语权（对上游议价能力、对下游定价能力、应收账款/应付账款周转对比）"
            "\n- 竞争格局（寡头/垄断/完全竞争？主要竞争对手及相对优劣势）"
            "\n- 行业地位决定业绩弹性：龙头享受确定性溢价，追赶者需证明α能力。"
            "\n"
            "\n**② 技术壁垒与护城河**"
            "\n- 技术壁垒：专利数量/质量、研发投入占比及资本化率、核心技术人员背景。"
            "\n- 护城河来源判断（选其一或组合）："
            "\n  · 成本优势：规模效应/工艺领先/资源垄断→毛利率持续高于同行。"
            "\n  · 品牌/渠道：客户粘性/转换成本/渠道深度→费用率趋势和复购率。"
            "\n  · 技术/专利：独家工艺/行业标准制定/专利墙→新产品迭代节奏和竞品差距。"
            "\n  · 网络效应：用户越多壁垒越高（平台型/生态型公司）。"
            "\n- 护城河是宽是窄？是加深还是变浅？趋势比静态更重要。"
            "\n"
            "\n**③ 基本面财务健康度**"
            "\n- 成长性：营收增速(vs行业)、扣非净利润增速、增速趋势（加速/稳定/放缓）。"
            "\n- 盈利能力：ROE及杜邦分解（净利率/周转率/杠杆率驱动），毛利率与净利率趋势，"
            "\n  核心判断：高ROE是否可持续？靠净利率驱动优于靠杠杆驱动。"
            "\n- 盈利质量：经营现金流/净利润 > 0.8 为健康，< 0.5 需警惕（利润是否为纸面富贵）。"
            "\n- 财务安全性：资产负债率、有息负债/EBITDA、商誉/净资产、大股东质押比例。"
            "\n- 估值匹配：当前PE/PB历史分位、PEG、前向PE，判断是否处于消化期。"
            "\n- 近期催化剂：机构一致预期EPS变动方向、业绩预告/财报披露节奏。"
            "\n"
            "\n📊 A股财务要点（CAS准则）：PE中位数30-50x为常态，对标同行业A股横向比较。"
            "\n财报时效性：一季报4月底/半年报8月底/三季报10月底/年报4月底。"
            "\n"
            "\n🔧 工具调用顺序：get_industry_comparison→get_fundamentals→get_profit_forecast(curr_date必传)→get_balance_sheet→get_cashflow→get_income_statement。"
            "\n"
            "\n📋 报告结构："
            "\n### 一、行业景气度（所处阶段、阶段优先级判断、宏观变量、反弹vs反转判定）"
            "\n### 二、个股行业地位与技术壁垒（市场份额、竞争格局、护城河来源及趋势、产业链话语权）"
            "\n### 三、个股基本面分析（成长性/盈利能力/盈利质量/财务安全性/估值匹配度）"
            "\n### 四、综合研判（估值匹配度、核心催化、风险分类：行业/个股/消化/范式迁移、关键观测指标）"
            "\n"
            "\n📋 必采清单：行业阶段/景气度及阶段优先级判断、行业趋势及驱动/压制、公司市场份额与竞争格局、护城河来源与趋势、产业链话语权、营收增速(vs行业)、扣非净利润增速及趋势、ROE及杜邦分解、毛利率/净利率趋势、经营现金流/净利润、资产负债率/商誉/质押、PE/PB历史分位及PEG、一致预期EPS、催化剂与风险列表。"
            "\n" + get_language_instruction()
        )

        # Inject evolution context if available
        evo_ctx = state.get("evolution_context")
        if evo_ctx:
            system_message += f"\n\n---\n## 自进化上下文\n{evo_ctx}\n---"

        # Inject industry-board context (行业榜) when available
        industry_ctx = state.get("industry_heatmap", "")
        if industry_ctx:
            system_message += (
                f"\n\n---\n## 行业热度上下文（最新行业榜）\n{industry_ctx}"
            )
            hot_stocks_ctx = state.get("hot_sector_stocks", "")
            if hot_stocks_ctx:
                system_message += f"\n热门行业龙头：{hot_stocks_ctx}"
            system_message += "\n（请结合该股所处行业的β环境研判，行业共振可放大个股信号；仅供参考）\n---"

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}.\n{system_message}"
                    "For your reference, the current date is {current_date}. {instrument_context}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)

        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "fundamentals_report": report,
        }

    return fundamentals_analyst_node
