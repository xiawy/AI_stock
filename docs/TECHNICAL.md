# AI Stock 技术文档

> 版本：v0.5.14 ｜ Python ≥ 3.10 ｜ 许可证：Apache-2.0

---

## 1. 技术栈总览

| 层 | 技术 |
|---|---|
| 编排 | LangGraph（StateGraph + 条件边 + SqliteSaver 断点） |
| LLM 接入 | langchain-openai / langchain-anthropic / google-genai 封装 / claude-agent-sdk（可选） |
| 数据处理 | pandas、stockstats（技术指标） |
| A 股数据 | mootdx（通达信 TCP 协议）、腾讯/东财/新浪/同花顺/财联社/百度公开 HTTP 接口 |
| 向量库 | chromadb（可选依赖，未装自动降级） |
| CLI | typer、questionary、rich |
| 后端 | FastAPI、SQLAlchemy 2.0、Alembic、PyJWT、bcrypt、SQLite |
| 前端 | Vue 3.5、Vite 6、Element Plus、Pinia、ECharts 5、Axios |
| 测试 | pytest（389 通过 / 1 失败 / 14 跳过，见 §12） |

## 2. 系统架构

```
┌─────────────────────────── 用户入口层 ───────────────────────────┐
│  CLI (ai-stock)   │  Vue3 前端 :5173  │  APScheduler 定时任务    │
└────────┬──────────┴─────────┬─────────┴──────────┬──────────────┘
         │                     │ FastAPI :8000       │
         │                     │  JWT / 任务管理      │
         │            ┌────────┴────────┐   ┌────────┴─────────┐
         │            │ web/ 桥接层      │   │ pipeline/ 模块    │
         │            │ runner 守护线程  │   │ 新闻影响 10 步流水 │
         │            │ progress tracker│   │ 采集→打分→辩论→推荐│
         │            └────────┬────────┘   └────────┬─────────┘
┌────────┴─────────────────────┴─────────────────────┴─────────────┐
│                    核心引擎 ai_stock/graph                        │
│  LangGraph StateGraph：                                          │
│  6 分析师(ReAct+工具) → QualityGate → Bull⇄Bull辩论 →             │
│  ResearchManager → Trader → 风险三方辩论 → PortfolioManager       │
│  ├─ checkpointer: per-ticker SQLite 断点                         │
│  ├─ reflection / signal_processing: 评级解析与反思                │
│  └─ evolution/: 进化包装（三层记忆 + 策略加载 + 权重分配）          │
├──────────────────────────────────────────────────────────────────┤
│  agents/: 角色节点工厂    llm_clients/: 厂商适配 + role_llms      │
├──────────────────────────────────────────────────────────────────┤
│                数据层 ai_stock/dataflows/a_stock.py               │
│  mootdx(TCP:7709) → 腾讯(GBK) → 东财(节流1s) → 新浪 → 同花顺 →    │
│  财联社 → 百度股市通   ｜ point-in-time 防护 ｜ CSV 缓存           │
├──────────────────────────────────────────────────────────────────┤
│  记忆系统 agents/utils/memory.py + performance.py（结算/统计）     │
│  数据目录 ~/.tradingagents/{cache, logs, memory, evolution_data}  │
└──────────────────────────────────────────────────────────────────┘
```

**关键设计原则 — 复用而非复制**：`backend` 与 `cli`、`web` 共享同一份引擎代码（backend 通过 `sys.path` bootstrap 桥接），共享同一份 `~/.tradingagents` 数据目录，断点与历史全局通用。

## 3. 目录结构

```
ai-stock/
├── ai_stock/                  # 核心引擎包
│   ├── graph/                 # LangGraph 编排（见 §4）
│   ├── agents/                # 角色节点：analysts/ researchers/ managers/ trader/ risk_mgmt/
│   │   ├── quality_gate.py    #   数据质量门控（两层校验）
│   │   ├── schemas.py         #   结构化输出（ResearchPlan/TraderProposal/PortfolioDecision）
│   │   └── utils/             #   memory(记忆日志) / rating(评级解析) / agent_states(State)
│   ├── dataflows/             # 数据层（a_stock 为主力，alpha_vantage*/y* 为海外备选）
│   ├── evolution/             # 自进化层（Chroma 三层记忆/策略加载/权重分配）
│   ├── llm_clients/           # LLM 工厂：openai 兼容 8 家 + anthropic/google/azure/agent_sdk
│   ├── pipeline/              # 新闻影响流水线（10 步）
│   └── default_config.py      # 全部运行配置默认值（见 §10）
├── cli/                       # CLI 应用（typer）
├── web/                       # UI 无关的引擎支撑：runner/progress/history/pdf_export/stock_display
├── backend/                   # FastAPI 后端（独立 venv、SQLite、Alembic）
├── frontend/                  # Vue3 前端
├── custom_strategies/         # 用户自定义策略 Markdown（12 类目录）
├── tests/                     # pytest 测试套件
└── docs/                      # 本文档所在
```

## 4. 核心引擎：LangGraph 图结构

### 4.1 图拓扑（`graph/setup.py::GraphSetup.setup_graph`）

```
START → [Analyst₁ → ToolNode₁ → MsgClear₁]* → QualityGate
      → Bull ⇄ Bear（辩论，条件边控制轮次）→ ResearchManager
      → Trader → RiskMgr 汇总
      → Aggressive ⇄ Conservative ⇄ Neutral（风险辩论）
      → PortfolioManager → END
```

- 每个分析师配备独立 ToolNode（`create_react_agent` 模式）；MsgClear 节点在分析师间清空消息，**隔离上下文**（分析师互不可见，这是并行化的数据流依据）
- 辩论轮次由 `conditional_logic.py` 控制：`count >= 2 * max_debate_rounds`（多空）/ `3 * max_risk_discuss_rounds`（风险三方）
- `ROLE_KEYS` 定义 15 个合法角色名，供 `role_llms` 按角色指定模型

### 4.2 断点续跑（`graph/checkpointer.py`）

- per-ticker SQLite 文件，`thread_id = sha256(ticker:date)[:16]`
- `prepare_graph_run / finalize_graph_run / close_graph_run` 管理生命周期
- 恢复时 `checkpoint_step()` 读取已执行到的节点序号，跳过已完成节点
- 默认关闭（`checkpoint_enabled: False`），因为每节点写盘有开销

### 4.3 主编排类（`graph/trading_graph.py::TradingAgentsGraph`）

- `_build_role_llms`：角色级模型构建（带缓存复用，同角色同配置只建一次客户端）
- `_normalize_yfinance_ticker`：A 股代码 → Yahoo 后缀（记忆结算用）
- agent_sdk 降级：订阅额度调用失败时回落到 API 计费通道，**成对校验**避免半降级状态

### 4.4 结构化输出（`agents/schemas.py`）

- ResearchPlan / TraderProposal / PortfolioDecision 三个 Pydantic 模型 + `render()` 回 Markdown
- PortfolioDecision 输出五档评级 `Buy/Overweight/Hold/Underweight/Sell`
- 评级解析（`agents/utils/rating.py`）为**确定性正则解析**（非 LLM 判断），经多轮边界修复（中英文混排、词边界、防回溯）

## 5. 数据层设计

### 5.1 多源矩阵（`dataflows/a_stock.py`，约 2500 行）

| 数据 | 首选源 | Fallback | 备注 |
|---|---|---|---|
| 日线 OHLCV | mootdx（TCP 7709） | 新浪 HTTP K 线 | 800 根 ≈ 3 年；CSV 缓存 |
| 实时行情 | 腾讯（GBK 编码） | — | 快照类，历史复盘时带标注 |
| 财务三表 | 新浪 openapi | — | 按 `报告日 ≤ curr_date` 过滤 |
| 新闻 | 东财搜索 | 新浪、财联社 | jsonp 剥壳解析 |
| EPS 一致预期 / 热点 / 北向 | 同花顺 | — | |
| 龙虎榜 / 解禁 / 行业对比 / 资金流 | 东财 datacenter | — | `_em_get` 统一节流 1s+随机抖动 |
| 名称↔代码映射 | mootdx 双市场股票表 | — | 进程级缓存 |

**健壮性设计**（值得沿用）：
- mootdx 服务器探测 + 失败负缓存（300s 内不重试）+ BESTIP 保护上下文管理器
- 东财请求全局节流（1s + 随机抖动）防封禁
- 非 A 股代码（港股/美股）在入口处**显式报错**，绝不静默放行去查错数据
- 中文股票名输入自动 resolve（精确匹配 → 唯一包含匹配 → 多匹配报错列出候选）
- `safe_ticker_component`：代码进入任何路径/URL 前做白名单校验（路径穿越防护）

### 5.2 point-in-time 防未来函数

- `_MARKET_TZ = UTC+8`：一切"今天"的判断按市场时区（`_market_today()`），与主机时区解耦
- `_is_historical(curr_date)`：复盘历史日期时，实时资金流自动截断、快照数据顶部注入显著警示
- OHLCV 加载后统一 `df[df["Date"] <= curr_date]` 截断
- ⚠️ 但存在两处遗留本地时区调用，见 §11 B5

### 5.3 缓存

- 日线 CSV：`~/.tradingagents/cache/{code}-astock-daily.csv`，当日命中则跳过 mootdx，缺口用新浪增量补齐后回写
- 名称映射、mootdx 客户端：进程级单例

## 6. LLM 客户端体系

### 6.1 双档模型 + 角色覆盖

- `quick_think_llm`：分析师（7 个工具角色）、辩手、Trader——量大、单价敏感
- `deep_think_llm`：Research Manager、Portfolio Manager——量小、质量敏感
- `role_llms`：按角色覆盖（如 bull 用 DeepSeek、bear 用 Qwen，制造真实观点对抗）
- `deep/quick_think_provider_override`：指定档位走 claude_agent_sdk（个人订阅额度）

### 6.2 厂商适配（`llm_clients/factory.py`）

- OpenAI 兼容通道 8 家：openai / deepseek / qwen(dashscope) / glm / minimax / xai / openrouter / ollama
- anthropic 通道（对第三方模型有宽松 max_tokens 兜底）
- claude_agent_sdk（可选 extras `[agentsdk]`）：走本机 claude CLI 订阅额度；与 `ANTHROPIC_API_KEY` 互斥（检测到共存直接报错，防止悄悄走计费）
- google 通道**结构性缺席**：mootdx 钉死 `httpx<0.26`，与 google-genai 要求的 `httpx>=0.28` 冲突，故无 `[google]` extras（详见 pyproject 注释，issue #87）

## 7. 记忆系统与绩效

- `TradingMemoryLog`（append-only Markdown，tmp+replace 原子写、batch_update、可选轮转）
- 生命周期：分析结束写 `pending` 条目 → N 交易日后 `_fetch_returns`（yfinance）结算 raw/alpha → REFLECTION 段落注入后续分析提示词
- `performance.py`：方向正确率（Hold 不计）、评级单调性检验、`MIN_MEANINGFUL_SAMPLE=20`
- ⚠️ 结算依赖 yfinance，国内网络受阻时 pending 永不结算，见 §11 B12/§13 O2

## 8. 进化层（`evolution/`，可选）

- `EvolutionWrapper` 包装每个图节点：执行前检索相似历史经验注入提示词，执行后写入经验库
- 三层记忆：semantic（`custom_strategies/*.md`，按角色/市场状态组织）/ episodic（Chroma 向量）/ working
- `market_regime` 识别牛/熊/震荡 → `weight_allocator` 决定注入哪些策略及权重
- chromadb 未安装 → `evolution_enabled` 自动降级 False（⚠️ 无日志提示，见 §11 B11）

## 9. Web / Backend / Frontend

### 9.1 web/ 桥接层（UI 无关）

- `runner.py`：守护线程流式消费 `graph.stream(stream_mode="values")`，驱动 ProgressTracker
- `progress.py`：11 阶段状态机 + `threading.Event` 暂停/停止门（暂停粒度=节点级）
- `history.py`：完成/未完成任务索引（原子写 + Windows 文件占用重试降级）
- `pdf_export.py`：Markdown→PDF（CJK 字体探测）；`stock_display.py`：代码+名称归一化

### 9.2 backend/（FastAPI）

- `AnalysisTaskManager`：内存注册表 `task_id(uuid4.hex) → ProgressTracker` + `analysis_tasks` 表持久化任务元数据与 LLM 配置快照
- `app/core/trading/__init__.py::bootstrap()`：`sys.path` 注入项目根，复用 `web/` 与 `ai_stock/`
- 惰性导入：API 进程可在未装分析栈依赖时启动，仅认证类请求正常服务
- lifespan：`init_db()` 建表 + pipeline service 初始化（失败仅 warning 非致命）
- API 路由：auth / analysis（start·status·result·pause·resume·stop·resume-checkpoint）/ stocks（search·quote·kline）/ history（含 md/pdf 导出）/ watchlist / impact / recommendation

### 9.3 frontend/（Vue3）

- 视图：Login/Register/Dashboard/Analysis/HistoryReport/ImpactRanking/StockRecommendation
- 组件：StockSearch / ProgressTracker(2s 轮询) / ReportViewer(markdown-it) / KlineChart(ECharts) / StockCard / ImpactDetail / AppHeader
- Pinia 持久化 token，路由未登录守卫，axios 拦截器自动附 JWT
- Vite 代理 `/api → localhost:8000`

## 10. 配置体系

全部默认值集中在 `ai_stock/default_config.py::DEFAULT_CONFIG`，环境变量优先。关键项：

| 配置 | 默认 | 说明 |
|---|---|---|
| `llm_provider` / `deep_think_llm` / `quick_think_llm` | env 驱动 | `.env` 中 `LLM_PROVIDER` + `LLM_MODEL` 统一控制两档 |
| `backend_url` | None | 第三方中转/代理网关 |
| `max_tokens` | None | 单回复上限（报告截断通常撞此值，`TRADINGAGENTS_MAX_TOKENS`） |
| `role_llms` | {} | 按角色指定 provider/model |
| `deep/quick_think_provider_override` | None | 档位走 claude_agent_sdk |
| `agent_sdk_model` / `agent_sdk_quick_model` | "opus" / "sonnet" | 订阅档模型别名（不写死版本号防过期） |
| `checkpoint_enabled` | False | 断点续跑开关 |
| `market_lookback_days` | None | 技术面回看窗口（None=模型默认~30） |
| `max_debate_rounds` / `max_risk_discuss_rounds` | 1 / 1 | 辩论轮数 |
| `data_vendors` | 全 a_stock | 五类数据供应商路由（a_stock/alpha_vantage/yfinance） |
| `evolution_enabled` | True | 进化层（chromadb 缺失自动降级） |
| `review_schedule` | ["Tue","Thu","Sun"] | 进化复盘调度 |
| `results_dir` / `data_cache_dir` / `memory_log_path` | `~/.tradingagents/...` | 可被 `TRADINGAGENTS_RESULTS_DIR` 等环境变量覆盖 |
| `output_language` | Chinese | 报告语言（内部辩论保持英文以利推理质量） |

---

## 11. 代码审查发现（Bug 清单）

> 审查范围：全部核心模块 + 测试套件运行（389 通过 / 1 失败 / 14 跳过）。按严重程度排序；行号以 v0.5.14 为准。

### 高 — 影响输出正确性

**B1. quality_gate 对未运行的分析师误判 F，少选分析师时 LLM 复审被误杀**
`ai_stock/agents/quality_gate.py` L126-169
`REPORT_FIELDS` 固定遍历全部 6 个分析师字段做硬检查；未选中的分析师报告为空 → 判 F（"报告为空"）。LLM 复审触发条件是 `fail_count < 4`，因此**用户只选 ≤2 个分析师时复审永远跳过**。且质量摘要向下游 Bull/Bear/Manager 声称"多位分析师报告为空"，误导辩论证据权重。
*修复*：从 state 读取实际选中的分析师集合，仅对已运行项做硬检查与复审；复审 prompt 的"6 位分析师"改为动态。

**B2. 新浪 K 线/财报接口的北交所前缀错判**
`ai_stock/dataflows/a_stock.py` L634（`_sina_kline_fallback`）、L1179（`_get_financial_report_sina`）
两处用 `prefix = "sh" if code.startswith("6") else "sz"`，北交所代码（8/43/92 开头）被错判 `sz`，新浪返回空数据。同文件 L44 `_get_prefix()` 已正确处理（92/8→bj，见 issue #85 注释），`_fetch_news_sina` L1360 也已用正确版本——同一文件三种写法不一致。
*影响*：北交所股票的 K 线增量补充与三大财报静默失败（mootdx 也失败时技术面完全无数据）。
*修复*：两处改用 `_get_prefix(code)`（需验证新浪接口对 `bj` 前缀的支持面，或对北交所显式走其他源）。

**B3. get_dragon_tiger_board 变量跨 try 块作用域 NameError 被吞**
`ai_stock/dataflows/a_stock.py` L2244（`if data:`）、L2294（`buy_data`/`sell_data`）
`data/buy_data/sell_data` 均在 try 块内赋值。最常见路径（未上榜 → `if data` 为 False → 跳过 buy/sell 赋值）下，第 3 段机构动向 L2294 必然抛 NameError 被 `except Exception: pass` 吞掉；第 1 段查询异常时 L2244 同样 NameError。编程错误被静默掩盖，部分失败场景下席位/机构动向静默缺失且无从排查。
*修复*：函数开头预定义 `data = buy_data = sell_data = []`；裸 `except: pass` 至少补 `logger.debug`。

**B4. web/history.py 硬编码数据目录，忽略环境变量**
`web/history.py` L19、L23-24、L140
`_results_dir()` 硬编码 `~/.tradingagents/logs`，`_INCOMPLETE_TASKS_FILE` 硬编码 `~/.tradingagents/incomplete_tasks.json`；但写入侧走 `config["results_dir"]`（支持 `TRADINGAGENTS_RESULTS_DIR`）。`_checkpoint_step` 用 `DEFAULT_CONFIG["data_cache_dir"]` 同样脱离运行配置。
*影响*：自定义目录用户（含 Docker 卷映射场景）在 Web 历史页看不到自己的报告，续跑任务读错 checkpoint。
*修复*：三处改为读 `DEFAULT_CONFIG` 对应键（模块已导入该常量，成本为零）。

**B5. 缓存新鲜度与北向快照使用主机时区**
`ai_stock/dataflows/a_stock.py` L758-759（`mtime.date() == datetime.now().date()`）、`get_northbound_flow` 快照日期
文件其余部分严格用 `_market_today()`（UTC+8，注释明确警告主机时区陷阱），这两处漏网。主机在 UTC+9 以东时当天缓存被判过期 → 每次穿透缓存重拉 mootdx；西半球则反向缓存陈旧数据。
*修复*：改用 `datetime.now(_MARKET_TZ).date()`。

### 中 — 功能缺口 / 行为不符

**B6. CLI 分析师菜单缺口（4/6）**
`cli/models.py` L6-11：`AnalystType` 枚举仅 market/social/news/fundamentals，缺 policy 与 hot_money。引擎（`graph/setup.py`）支持全部 6 个，CLI 用户无法选择政策分析师与游资追踪师——恰是 A 股特化卖点。
*修复*：补枚举值并核对 `ANALYST_ORDER` 下游映射。

**B7. get_industry_comparison 声称 top/bottom 实际只有涨幅榜**
`ai_stock/dataflows/a_stock.py` L2442-2457
东财接口 `po=1` 降序取 100 行，循环 `if i >= top_n * 2 - 1: break` 只输出涨幅前 2·top_n 名，尾注却写 "showing top/bottom {top_n}"。领跌行业从未出现，行业对比信息不完整。
*修复*：全量取回后手动分片输出头部 top_n 与尾部 top_n。

**B8. CHANGELOG 与 pyproject 版本不一致**
`tests/test_version_consistency.py::test_changelog_top_entry_matches_pyproject` 当前失败（pyproject=0.5.14）。发布流程需补 CHANGELOG 顶部条目。

### 低 — 健壮性隐患

**B9. backend 重启后 running 任务成孤儿**
`backend/app/services/analysis_service.py`：task→tracker 仅在内存，重启后 DB 中 `status=running` 的行无进程对应，前端永远显示"进行中"。
*修复*：lifespan 启动时将 DB 中 running 行批量改标 `interrupted`。

**B10. backend SECRET_KEY 有默认值**
`backend/app/core/config.py`：默认 `"dev-secret-key-change-me-in-production"`，忘配 `.env` 时 JWT 可被伪造。
*修复*：`DEBUG=false` 且未显式配置时拒绝启动。

**B11. evolution 降级静默无日志**
`graph/setup.py::_wrap_evolution` 与 chromadb 缺失降级路径：配置为 True 实际从未生效且无提示。
*修复*：降级时 `logger.warning` 一次性告知。

**B12. 记忆结算依赖 yfinance（国内环境永久 pending）**
`graph/trading_graph.py::_fetch_returns`：结算走 yfinance；国内网络受阻时 pending 条目永不结算、REFLECTION 与绩效统计空转。且 yfinance 位于核心依赖拖慢安装。
*修复*：结算改用 mootdx/腾讯历史行情（日线数据本已在 CSV 缓存）；yfinance 降级为 optional。

> 已排除的疑点：stats 回调在 LLM 构造器与 graph config 双重注册是否会双计 token/调用数——实验验证 LangChain 对同一 handler 实例去重，仅触发一次，**不是 bug**。

---

## 12. 测试

```powershell
# 项目根（引擎 + CLI + web 测试）
python -m pytest tests -q
# backend 测试（独立 venv）
cd backend; .venv\Scripts\python -m pytest tests -q
```

当前基线：**389 通过 / 1 失败（B8 版本一致性）/ 14 跳过**（agentsdk、google 可选依赖未装）。测试覆盖亮点：评级解析边界、防未来函数守卫、市场前缀路由、断点续跑、ticker 安全处理、结构化输出、角色 LLM、输出 token 上限等。

## 13. 优化建议（按收益排序）

**O1. 分析师层并行化（性能，收益最大）**
现状 `setup.py` 将 6 个分析师串行链式连接，每个含多轮工具调用，是单次分析的主要耗时。数据流证据：分析师仅读被 MsgClear 隔离的 `state["messages"]` 与自身工具，互不读取报告；汇总发生在 Bull/Bear。可改为 LangGraph fan-out/fan-in：6 分支并行、汇合进 QualityGate。分析师阶段耗时可降至最慢分支（预计整体提速数倍）。注意：各分析师写独立 state 字段（天然无写冲突），但需给 messages 加 reducer 或各分支局部消息。

**O2. 记忆结算改用国内数据源**（同 B12）：解除国内环境记忆/绩效空转，同时给 yfinance 降级。

**O3. get_indicators 过滤非交易日行**：逐日输出中周末/节假日全为 N/A，浪费 token 且稀释模型注意力；输出前 dropna。

**O4. `DEFAULT_CONFIG` 浅拷贝加固**：`build_config` 等处以 `.copy()` 浅拷贝嵌套 dict（`data_vendors` 等）。当前代码以整体重新赋值方式修改所以安全，但属脆弱约定；改 `copy.deepcopy` 一行消除隐患。

**O5. evolution 与 quality_gate 的可用性感知**（同 B1/B11）：降级/门控都要知道"实际配置"而非"全量假设"。

**O6. Docker 补前后端服务**：现 Dockerfile 仅 CLI（`ENTRYPOINT ai-stock`）；可加 compose profile：backend（uvicorn）+ frontend（nginx 托管 dist 并反代 /api）。

**O7. pipeline 并发护栏**：新闻流水线多股打分/辩论并发时对 LLM 供应商无并发限制，易撞 rate limit；可加信号量或退避重试队列（已有 `_retry_call` 指数退避，可复用扩展）。

**O8. 历史索引分页与缓存**：`web/history.py::get_history()` 每次全量 `rglob` 扫描日志目录，报告量大后 Web 历史页会变慢；可加 mtime 缓存或索引文件。

## 14. 关键设计决策备忘

| 决策 | 理由 |
|---|---|
| 无 `[google]` extras | mootdx 钉 `httpx<0.26` 与 google-genai 的 `httpx>=0.28` 结构性冲突（#87）；uv 全量 lock 会连累所有用户 |
| mootdx ≥0.11.7 | 首个兼容 pandas≥2.3 的版本，放宽会解析到要求 pandas<1.3.5 的远古版本报出误导性冲突（#87） |
| 评级解析用正则而非 LLM | 确定性、零成本、可测试（tests 有大量边界用例） |
| 内部辩论英文、报告输出中文 | 推理质量优先；`output_language` 只作用于最终报告 |
| 决策不携带价位/仓位 | 研究定位，避免被当交易信号使用（schemas 层面就不含这些字段） |
| agent_sdk 与 API key 互斥护栏 | 防止用户以为在用订阅额度实际走 API 计费 |
| per-ticker SQLite checkpoint 而非全局 | 隔离并发任务，避免锁竞争 |

---

*本文档对应代码版本 v0.5.14。*
