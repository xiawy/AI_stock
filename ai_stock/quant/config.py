"""Quant trading subsystem configuration (V3.0).

All constants for the 量化交易 decision system: scheduling, position
sizing, risk thresholds, message-queue behaviour, cache TTLs and broker
fees.  Storage lives in its own SQLite file (see ``db.py``); Redis is
optional and gracefully degrades to a SQLite queue.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Queues (Redis List keys / SQLite queue names)
# ---------------------------------------------------------------------------

SELECTION_QUEUE = "selection_control"
BUY_QUEUE = "buy"
HOLD_QUEUE = "hold"
RISK_QUEUE = "risk"
ORCHESTRATOR_QUEUE = "orchestrator"

ALL_QUEUES = (
    SELECTION_QUEUE,
    BUY_QUEUE,
    HOLD_QUEUE,
    RISK_QUEUE,
    ORCHESTRATOR_QUEUE,
)

# ---------------------------------------------------------------------------
# Scheduling — scheduler callbacks ONLY enqueue tasks, never run business
# logic (see 设计文档 §5.1).  All times are local (A-share) time.
# ---------------------------------------------------------------------------

# 挑选自选: 每日 7:00 / 12:00 / 18:00 (盘前事件扫描 / 午间修订 / 盘后复盘)
SELECTION_SCHEDULE = ["07:00", "12:00", "18:00"]

# 自选买入: 开盘期间每 10 分钟 (与持仓扫描同频)
# A 股缩量回踩等信号的有效窗口常只有 15~30 分钟, 30 分钟槽位会系统性错过
# (P1-2/T8: 一天 8 次机会窗口 → 24 次)
BUY_SCAN_INTERVAL_MINUTES = 10

# 持仓维护: 开盘期间每 10 分钟
HOLD_SCAN_INTERVAL_MINUTES = 10

# 异步风控兜底扫描: 每 5 分钟
RISK_SCAN_INTERVAL_MINUTES = 5

# 每日运行报告: 15:35 (收盘后)
DAILY_REPORT_AT = (15, 35)

# LLM 调用保护 (quant 子系统专用, 见 llm_helper.create_quant_llm):
# 单次请求超时与重试上限 — 无超时的 invoke 可无限阻塞, 会把整个选股步骤
# 拖到 3600s 流程超时熔断, 白白浪费整个调度槽位 (行业榜/自选池停更)
LLM_REQUEST_TIMEOUT = 300    # 秒, 单次 LLM 请求总超时 (深析批量输出留足余量)
LLM_MAX_RETRIES = 1          # 超时后重试 1 次, 失败即宁缺毋滥降级

# A-share trading sessions (local time): 9:30-11:30 / 13:00-15:00
MORNING_SESSION = (9, 30, 11, 30)
AFTERNOON_SESSION = (13, 0, 15, 0)

# ---------------------------------------------------------------------------
# Position & pool rules
# ---------------------------------------------------------------------------

# 单次建仓金额 = 可用资金 × 20%
BUY_POSITION_RATIO = 0.20
# 硬性规定: 持仓不能超过 3 支
MAX_HOLDING_COUNT = 3
# 单轮 buy_scan 最多评估多少只自选股（按 dyn_confidence 降序）。
# 「评估数量」与「开仓数量」必须解耦：原实现用 MAX_HOLDING_COUNT 截断
# 扫描池，等于「最多持仓 3 支」被写成了「每轮只看 3 只」。池内 22 只时
# 有 19 只全年不被看一眼，2026-10-20 实测 4 只当天触发信号的标的
# （华明装备/中航西飞/威海广泰/中航机载）排名 6~22，全部漏掉。
BUY_SCAN_MAX_CANDIDATES = 30
# 单轮真正启动 flow 的上限 = 空仓位 + 该溢出值。溢出用于允许「多只并行
# 走深度验证」，先通过的先建仓；最终仍由 pre_trade_check 的持仓上限兜底。
BUY_SCAN_FLOW_OVERFLOW = 2
# 同票冷却（分钟）：二次验证被拒后，当日不再为该票启动买入流程。
# 原实现每 10 分钟对同一只票重复跑 LLM 深度验证，67 次里 27 次是同一批票
# 的重复拒绝，既烧钱又让决策日志失去信息量。
BUY_REJECT_COOLDOWN_MINUTES = 240
# 二次验证的风险收益比下限（< 该值直接拒绝；0 关闭该校验）
MIN_RISK_REWARD_RATIO = 1.5
# 单只持仓市值占账户总资产上限 (软约束, 风控扫描用)
SINGLE_POSITION_MAX_PCT = 0.30
# 单次建仓比例的硬上限: 基础比例 × 最高动态系数。
# pre_trade_check 用它做金额校验的兜底口径, 与 position_open 的
# 「BUY_POSITION_RATIO × coef」共用同一事实来源 (P1-1: 原代码把上限
# 写死成 20%, 导致 coef=1.2 的高置信标的 100% 被拒)。
POSITION_RATIO_HARD_CAP = BUY_POSITION_RATIO * 1.2
# 自选池观察期 (交易日), 到期自动 expired (BODY 档基准, 分层见下)
OBSERVE_EXPIRE_TRADING_DAYS = 7
# 观察期按阶段分层 (T5/P1-4): 主线鱼头给足 10 天, 鱼身 7 天, 鱼尾立即出池
OBSERVE_EXPIRE_DAYS_BY_STAGE = {"HEAD": 10, "BODY": 7, "TAIL": 3}
# 深度分析置信度低于该值不入池 (宁缺毋滥)
MIN_ENTRY_CONFIDENCE = 6.0

# 同一行业 (申万一级/板块归口) 的最大同时持仓数 (T2 行业集中度硬约束)。
# 历史教训: 池内 8 只标的 5 只属军工产业链, 3 个持仓名额会一次性买成
# 单 beta 押注。1 = 同行业只允许一只。
INDUSTRY_CONCENTRATION_MAX = 1

# ---------------------------------------------------------------------------
# 选股集群: 自上而下 (宏观事件 → 生命周期 → 三大支柱) 筛选常量 (§7.1)
# ---------------------------------------------------------------------------

MAX_INDUSTRIES_OUTPUT = 4            # 生命周期定位后最终输出行业数 (Top 4)
STOCK_SELECT_INDUSTRIES = 3          # 进入个股精选的行业数 (优化点 1: 控制 LLM 调用次数)
STOCKS_PER_INDUSTRY = 3              # 每行业精选入池股票数 (合计约 9 只)
MIN_STOCK_COMPREHENSIVE_SCORE = 5.0  # LLM 三大支柱综合分硬门槛, 低于直接剔除
INDUSTRY_STOCK_POOL_SIZE = 20        # 每行业取流动性前 20 成分股作候选
MAX_LIMIT_UP_WAVES = 8               # 涨停潮题材最多保留条数 (辅助信号)
MACRO_NEWS_DAYS = 3                  # 宏观事件解析的新闻窗口 (日)
MAX_SCAN_CANDIDATES = 12             # 行业扫描候选上限 (事件映射优先, 涨幅榜补足)

# 行业综合排名权重 (供需第一性原理 × 生命周期矩阵 × 上游传导 × 涨停潮):
# 综合分 = 供需失衡分×W1 + 阶段分×W2 + 上游传导加分×W3 + 涨停潮加分×W4,
# 另叠加扩产周期/进入壁垒微调, 各项归一化后映射到 0-10 (见 IndustryScanAgent)
WEIGHT_IMBALANCE = 0.40      # 供需失衡分 (0-10, 10=极度供不应求) 权重
WEIGHT_STAGE = 0.30          # 生命周期阶段静态分权重 (八档矩阵, 保留原逻辑)
WEIGHT_UPSTREAM = 0.15       # 上游传导发散加分权重 (快人一步的产业链发散)
WEIGHT_WAVE = 0.15           # 涨停潮右侧确认加分权重 (原阶段修正器并入加权)
WEIGHT_CYCLE = 0.05          # 扩产周期微调权重 (周期越长供需逻辑越持久)
WEIGHT_BARRIER = 0.05        # 进入壁垒微调权重 (壁垒越高超额利润越难稀释)
BONUS_UPSTREAM = 1.5         # 被其他候选行业点名为上游时的固定加分
BONUS_WAVE = 1.5             # 当日出现涨停潮行业的加分 (右侧确认信号)
IMBALANCE_STAGE_ESCALATION = 6.0  # 供需失衡分达该阈值且阶段分非正 → 强制上调爆发前期

# 行业榜 (前端展示): 筛选自选时生命周期排序后的前 N 个行业作为行业榜,
# 每个行业展示前 M 只龙头股 (精选行业取三大支柱评估排序, 其余取流动性前 M)
INDUSTRY_BOARD_SIZE = 10
LEADER_STOCKS_PER_BOARD = 10

# 上游传导二次验证: Top 行业点名的上游未上榜时, 能对账板块库则附加展示在行业榜
# (不强制入 Top, 不参与个股精选); 失衡分按衰减系数保守计分 (确定性弱于主行业)
MAX_TRANSMISSION_BOARDS = 3
TRANSMISSION_DECAY = 0.7

# ---------------------------------------------------------------------------
# 主题雷达 + 动态置信度 + 鱼尾清理 (旁路注入增强, 全部可降级)
# ---------------------------------------------------------------------------

# 微观异动雷达: 全市场扫描 "放量 + 温和上涨" 的细分主题隐形冠军 (scan_micro_anomalies)
RADAR_CHANGE_PCT_MIN = 3.0        # 涨幅下沿 (%): 低于此非异动
RADAR_CHANGE_PCT_MAX = 8.0        # 涨幅上沿 (%): 高于此已透支, 不追
RADAR_VOLUME_RATIO_MIN = 2.0      # 量比门槛 (>2 视为显著放量)
RADAR_MARKET_CAP_MAX = 300e8      # 总市值上限 (元): 只嗅探中小市值弹性标的
RADAR_MIN_CLUSTER = 2             # 单主题最少异动家数 (少于此不成气候, 丢弃)
RADAR_MAX_THEMES = 6              # 雷达最多保留主题数 (控制注入体量)
RADAR_STRONG_CLUSTER = 6          # 簇规模归一基准: strength=min(1, 家数/该值)

# 动态置信度 dyn_confidence (0-1, 旁路字段; 原 confidence 0-10 LLM 分不动):
# 入池时 dyn = (LLM分/10)*W_LLM + 雷达强度*W_RADAR; 之后每日衰减/增强
CONF_DEFAULT = 0.5                # 缺省值 (旧数据/字段缺失退化基准)
CONF_LLM_WEIGHT = 0.6             # LLM 综合置信度权重
CONF_RADAR_WEIGHT = 0.4           # 雷达强度权重
CONF_DECAY_FACTOR = 0.95          # 超期未验证的衰减系数
CONF_REINFORCE_FACTOR = 1.1       # 主题今日再现雷达的增强系数
CONF_DECAY_AFTER_DAYS = 5         # 距上次确认超过该天数才衰减
CONF_KICK_THRESHOLD = 0.25        # 低于该值自动移出自选池 (置信度衰竭)
# 主线突破通道开启的最低 dyn_confidence。
# 0.7 与 dyn 实际分布严重不匹配 (实测池内中位数 0.42, 最高 0.74),
# 等于把唯一一条「追强」通道永久关闭 (P1-2)。
CONF_BREAKOUT_THRESHOLD = 0.55
# 相对门槛: dyn 排名前该比例内也可进突破通道 (与绝对阈值取或)。
# 绝对阈值会随雷达评分口径漂移, 相对排名更稳。
CONF_BREAKOUT_TOP_PCT = 0.30
# 清理官移除需连续命中的次数 (P1-4): LLM 单次采样波动不得直接移除标的,
# 首次命中只告警留痕, 连续 N 次才真正移出。
KICK_CONFIRM_ROUNDS = 2

# 动态仓位系数 (在原 BUY_POSITION_RATIO 基础上叠加, 只影响总额度):
# pool_conf>=POS_HIGH_CONF 且 stage=BODY -> 1.2; >=POS_MID_CONF -> 1.0; 否则 0.5
# POS_MID_CONF 与 CONF_DEFAULT 对齐 (0.5): 保证旧池行/字段缺失的默认档退化为
# 原 20% 固定仓位 (§9 兼容保证), 仅被维护官衰减到默认档以下 (<0.5) 才减半。
POS_HIGH_CONF = 0.8
POS_MID_CONF = 0.5
POS_COEF_HIGH = 1.2
POS_COEF_MID = 1.0
POS_COEF_LOW = 0.5

# ---------------------------------------------------------------------------
# 波动率目标仓位 (T1)
# ---------------------------------------------------------------------------
# 固定「可用资金 × 20%」的问题是: 同样 20% 仓位, 买年化波动 40% 的票和 25% 的票
# 承担的风险完全不同。改为按风险预算定量:
#   qty = (总资产 × RISK_BUDGET_PER_TRADE) / (STOP_LOSS_ATR_MULT × ATR)
# 使每笔「触发止损时的亏损金额」趋同 (默认 1% 总资产), 再由资金上限二次约束。
VOL_TARGET_SIZING_ENABLED = True
RISK_BUDGET_PER_TRADE = 0.01      # 单笔风险预算 = 总资产 × 1%
# 组合级风险预算总闸: 账户回撤触及阈值 → 仓位系数打对折 (从源头降杠杆)
PORTFOLIO_DD_COEF_THRESHOLD = 0.06    # 回撤 6% 起
PORTFOLIO_DD_COEF_FACTOR = 0.5        # 系数 × 0.5

# ---------------------------------------------------------------------------
# 技术入场通道 (P1-2: 补「趋势跟随」维度)
# ---------------------------------------------------------------------------
# 原三个信号 (BOLL 缩量止跌 / MACD 底背离 / 缺口回踩缩量) 全部是均值回归型,
# 在单边上涨行情里永远等不到缩量回踩 → 实测池内 0/7 命中。新增趋势动量通道:
# close > MA20 且 MA20 上翘 且 量能 > 1.2×20日均量 且 RSI ∈ [50, 75] (追强不追妖)。
TREND_MA_WINDOW = 20
TREND_MA_SLOPE_MIN = 0.002       # MA20 相对 3 日前的最小上翘幅度 (0.2%)
TREND_VOLUME_RATIO = 1.2         # 末根量能 / 20 日均量
TREND_RSI_MIN = 50.0
TREND_RSI_MAX = 75.0
# MACD 背离的「新低」比较窗口 (根)。原实现只在 5 根里比较 ≈ 只跟最近 4 天比,
# 不构成背离语义。
MACD_DIVERGENCE_WINDOW = 25

# ---------------------------------------------------------------------------
# 止损 / 时间止损 (T4/T5)
# ---------------------------------------------------------------------------
# ATR 止损: 实际止损位 = min(成本 × (1-5%), 成本 - 2×ATR), 让每笔最大亏损
# 与波动率挂钩; 确认根数从 2 降为 1 (原 2 根确认在急跌中实际亏损可达 8~12%)
STOP_LOSS_ATR_MULT = 2.0
STOP_LOSS_CONFIRM_BARS = 1
# 分层时间止损 (交易日): HEAD 给足 10 天, BODY 7 天; TAIL 0 = 不单独计时
TIME_STOP_DAYS_BY_STAGE = {"HEAD": 10, "BODY": 7, "TAIL": 0}

# ---------------------------------------------------------------------------
# 大盘择时门 (T3)
# ---------------------------------------------------------------------------
# 沪深300 收盘 > MA20 且 MA20 上翘 → risk_on, 否则只减不加 (禁止新建仓)。
# 熊市里信号同样会触发, 没有这一层系统会持续抄底。
MARKET_TIMING_ENABLED = True
MARKET_INDEX_CODE = "000300"
MARKET_INDEX_PREFIX = "sh"
MARKET_MA_WINDOW = 20
MARKET_REGIME_TTL = 1800         # 30 分钟缓存 (与买扫周期同量级)

# ---------------------------------------------------------------------------
# 交易标的硬过滤 (T9)
# ---------------------------------------------------------------------------
NEW_LISTING_MIN_BARS = 60        # 上市不足 60 个交易日的次新股不建仓

# 阶段批次 stage_batch: HEAD(鱼头)/BODY(鱼身)/TAIL(鱼尾)
STAGE_HEAD_CHANGE_PCT_MAX = 15.0  # 新主题且当日涨幅<此值 -> HEAD, 否则 BODY

# 精准鱼尾检测 (is_tail_phase): 高位放量滞涨, 四条同时成立才判鱼尾
# 注: 旧版第 4 条 "MA5 单日上移 > 1.5%" 与第 2 条 "近 5 日滞涨 < 3%" 在真实价格
# 序列上数学互斥 (需 close[-6] 暴跌后拉回), 检测器几乎永不触发。改为用 "已大涨
# (鱼身已成) + 现价贴窗口最高 (仍在顶部)" 两条刻画 "高位", 去掉与滞涨冲突的门槛。
TAIL_LOOKBACK_BARS = 30           # 判定回看 K 线数 (覆盖一轮完整拉升, 作 "已大涨" 基线)
TAIL_VOLUME_SURGE = 1.5           # 末根量能 > 前 7 日均量 * 该倍数 (放量)
TAIL_TURNOVER_MIN = 20.0          # 换手率门槛 (%) (可得时校验, 缺失退化为量能代理)
TAIL_GAIN_MAX = 0.03              # 近 5 日涨幅 < 3% (滞涨)
TAIL_PRIOR_RUNUP_MIN = 0.20       # 现价较窗口最低收盘累计涨幅 >= 20% (鱼身已成 = 真高位)
TAIL_HIGH_DIST_MAX = 0.03         # 现价距窗口最高 < 3% (仍贴顶, 未明显回落)

# ---------------------------------------------------------------------------
# Stop-loss / take-profit rules (multi-bar confirmed, never intraday spikes)
# ---------------------------------------------------------------------------

# 硬性止损: 亏损超 5% (连续两根 K 线或收盘确认)
STOP_LOSS_PCT = 0.05
# 时间止损: 买入后 5 个交易日最大涨幅 < 3% 且 ATR 低于阈值 → 清仓
TIME_STOP_TRADING_DAYS = 5
TIME_STOP_MIN_GAIN = 0.03
TIME_STOP_ATR_RATIO = 0.04  # ATR/price 低于 4% 视为无行情

# 移动止盈: 盈利超过 15% 后, 止损位上移到成本 +10%
TRAILING_STOP_TRIGGER = 0.15
TRAILING_STOP_FLOOR = 0.10

# ---------------------------------------------------------------------------
# Message queue behaviour
# ---------------------------------------------------------------------------

MQ_BACKEND = os.getenv("QUANT_MQ_BACKEND", "auto")  # auto / redis / sqlite
REDIS_URL = os.getenv("QUANT_REDIS_URL", "redis://localhost:6379/1")
MAX_ATTEMPTS = 3          # 每任务最大重试次数
LOCK_TTL_SECONDS = 600    # 任务抢占锁 TTL (processing 超时判定)
# 调度/事件幂等 key TTL: 必须 ≥ 2× 最长流程时长 (selection=3600s)。
# 旧值 3600 与 selection 流程超时相等, 边界上必然失效 (A4)。
IDEMPOTENT_TTL = 7200
RETRY_BASE_DELAY = 30     # 指数退避基础延迟 (秒)
SWEEP_INTERVAL = 30       # 超时巡检线程周期 (秒)
DELAYED_POLL_INTERVAL = 0.2  # 延迟任务迁移线程 sleep (秒)
CONSUME_TIMEOUT = 5       # brpop / 轮询阻塞超时 (秒)
HEARTBEAT_INTERVAL = 30   # consumer 心跳写入间隔 (秒)
DEAD_LETTER_ALERT_THRESHOLD = 1  # 死信队列非空即告警

# ---------------------------------------------------------------------------
# Cache TTL by data category (设计文档 §9.2)
# ---------------------------------------------------------------------------

CACHE_TTL = {
    "realtime_quote": 60,        # 实时行情 60s
    "fund_flow": 300,            # 资金流/龙虎榜 5min
    "news": 900,                 # 新闻/公告 15min
    "fundamentals": 3600,        # 财务/行业对比 1h
    "forecast": 6 * 3600,        # 一致预期/解禁日程 6h
    "ohlcv": 300,                # K线 5min (盘中滚动)
}
MEMORY_CACHE_MAXSIZE = 512

# ---------------------------------------------------------------------------
# Circuit breaker (数据源熔断)
# ---------------------------------------------------------------------------

CIRCUIT_FAILURE_THRESHOLD = 3   # 连续失败 N 次打开熔断
CIRCUIT_OPEN_SECONDS = 300      # 熔断窗口 5min, 之后 half-open 试探

# ---------------------------------------------------------------------------
# 数据源健康矩阵 + 退避 (P0-1 / A1)
# ---------------------------------------------------------------------------
# 单一供应商 (push2 clist) 被反爬封禁 6 周无人知晓, 因此:
# - 每个 (源, 接口) 记录成功率 / P95 延迟 / 最近成功时间, 供日报与告警消费
# - 连续失败 3 次进入指数退避 (5/15/60 min), 不再每 10 分钟硬撞 (硬撞只会加重封禁)
DATA_SOURCE_FAIL_THRESHOLD = 3
DATA_SOURCE_BACKOFF_STEPS = (300, 900, 3600)
DATA_HEALTH_SUCCESS_RATE_ALERT = 0.5   # 成功率跌破 50% 持续告警
DATA_HEALTH_LATENCY_WINDOW = 50        # 每接口保留的样本数
# 板块列表最短采样间隔 (秒): 进程内二次护栏, 叠加缓存 TTL 一起限流
BOARD_FLOW_MIN_INTERVAL = 300

# ---------------------------------------------------------------------------
# 业务级 SLO 告警 (A3: 告警只落库等于没有告警)
# ---------------------------------------------------------------------------
# 外部通道: 企业微信群机器人 / 通用 webhook (POST JSON {"text": "..."} )
# 未配置时仅落库 + 日志 (保持原行为, 不阻塞)
ALERT_WEBHOOK_URL = os.getenv("QUANT_ALERT_WEBHOOK", "").strip()
ALERT_WEBHOOK_TIMEOUT = 5
SLO_SCAN_EMPTY_STREAK = 3        # industry_scan 连续 N 次空 → WARNING
SLO_NO_TRADE_DAYS = 5            # 连续 N 个交易日无成交 → CRITICAL
SLO_SELECT_FALLBACK_RATIO = 0.5   # 选股降级占比 > 50% → WARNING

# ---------------------------------------------------------------------------
# 数据归档 (A6)
# ---------------------------------------------------------------------------
DECISION_LOG_RETENTION_DAYS = 90   # 决策日志保留天数 (此前永不清理)
FLOW_STATE_RETENTION_DAYS = 30     # 终态流程保留天数 (此前只增不减)

# ---------------------------------------------------------------------------
# News vector store
# ---------------------------------------------------------------------------

NEWS_RETENTION_DAYS = 30         # 新闻向量保留 30 天
NEWS_SEARCH_DEFAULT_DAYS = 7     # 默认检索时间窗口

# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

ALERT_SILENCE_SECONDS = 300      # 同类告警静默窗口 5min

# ---------------------------------------------------------------------------
# Simulated broker fees (A-share)
# ---------------------------------------------------------------------------

COMMISSION_RATE = 0.00025   # 佣金 万 2.5
COMMISSION_MIN = 5.0        # 单笔最低 5 元
STAMP_TAX_RATE = 0.0005     # 印花税 (卖出, 2023-08 减半后 万 5)
TRANSFER_FEE_RATE = 0.00001 # 过户费 十万分之一
SLIPPAGE_BPS = 10           # 模拟滑点 0.1% (市价单)

# lot size
LOT_SIZE = 100

# ---------------------------------------------------------------------------
# Account / system_config seed values
# ---------------------------------------------------------------------------
# 系统级执行账户标识: 引擎决策链路的既有执行主体 (多用户扇出的源头)
SYSTEM_USER = "system"

# 多用户模拟账户初始资金:
# - QUANT_INITIAL_CAPITAL (.env): 全员默认起始资金 (元)
# - QUANT_CAPITAL_FILE (JSON): 按用户覆盖, key 为 user_id 或 username,
#   value 为金额; 未列出的用户用默认值 (见 user_accounts.resolve_initial_capital)
INITIAL_CAPITAL_ENV = "QUANT_INITIAL_CAPITAL"
CAPITAL_FILE_ENV = "QUANT_CAPITAL_FILE"

# 实施步骤 §15-1: 初始化后 global_trade_enable=1 (允许模拟盘下单)。
# 历史教训: 9 月首装的库按 "0" 落库, 之后代码把默认值改成 "1", 但种子语义是
# 「已存在的键一律不覆盖」→ 新默认值永远到不了存量库, 直接导致 6 周 0 成交。
# 因此默认值变更必须走下面的 CONFIG_MIGRATIONS (A2/P0-2)。
SYSTEM_CONFIG_SEED = {
    "global_trade_enable": ("1", "全局交易开关: 0=禁止全部下单(一键熔断), 1=允许"),
    "max_daily_orders": ("50", "单日最大委托笔数"),
    # 250000 = 初始资金 25%, 与「基础 20% × 最高系数 1.2 = 24%」的仓位公式
    # 留 1% 缓冲; 原 200000 与高置信档位直接矛盾 (P1-1)
    "max_order_value": ("250000", "单笔最大委托金额(元)"),
    "allowed_symbols": ("", "允许交易的股票白名单(逗号分隔, 空=不限制)"),
    "initial_cash": ("1000000", "模拟盘初始资金(元)"),
    "max_drawdown_pct": ("0.20", "账户总回撤阈值(触发冻结新建仓)"),
    "max_daily_loss_pct": ("0.03", "单日最大亏损阈值(触发冻结新建仓)"),
    "trade_frozen": ("0", "账户级风控冻结: 1=只允许卖出, 0=正常"),
    # 大盘择时门: 1=开启 (沪深300 走强才允许新建仓)
    "market_timing_enable": ("1", "大盘择时门: 1=开启, 0=关闭 (只减不加)"),
    # 交易时段闸门: 1=开启 (非交易时段拒绝任何下单)。调度只在盘中入队, 但 flow
    # 异步执行会跨到午休/收盘之后, 没有这道闸门时盘后照样下单 (2026-10-20 发现)。
    "trading_session_guard_enable": ("1", "交易时段闸门: 1=非交易时段拒单, 0=关闭"),
}

# ---------------------------------------------------------------------------
# 配置 schema 版本与迁移表 (A2)
# ---------------------------------------------------------------------------
# 存储位置: quant_system_config['config_schema_version']
# 执行语义: 启动时若库版本 < CONFIG_SCHEMA_VERSION, 按 version 升序执行,
# 且**只迁移「值仍是旧默认值」的键** —— 运维手工改过的值绝不动。
CONFIG_SCHEMA_KEY = "config_schema_version"
CONFIG_SCHEMA_VERSION = 4

CONFIG_MIGRATIONS: tuple[dict, ...] = (
    {
        "version": 2,
        "description": "默认放开模拟盘交易开关, 并放宽单笔金额上限与仓位公式对齐",
        "changes": (
            # (key, 旧默认值集合, 新值)
            ("global_trade_enable", ("0",), "1"),
            ("max_order_value", ("200000",), "250000"),
        ),
    },
    {
        "version": 3,
        "description": "新增大盘择时门开关 (存量库补键)",
        "changes": (
            ("market_timing_enable", ("",), "1"),
        ),
    },
    {
        "version": 4,
        "description": "新增交易时段闸门开关 (存量库补键)",
        "changes": (
            ("trading_session_guard_enable", ("",), "1"),
        ),
    },
)

# Quant 数据库 URL (独立 SQLite; 测试/独立部署可用环境变量覆盖)
QUANT_DB_URL_ENV = "QUANT_DB_URL"

# 运行模式: 0=模拟盘(paper, 默认) 1=实盘(live)
# 灰度规则 (gray_scale=True) 仅在模拟盘生效 (§10.2 灰度→实盘 上线流水线);
# 实盘模式下灰度规则在 evaluate 中被跳过, 必须由人工将规则升级为正式规则后才触发。
LIVE_MODE = os.getenv("QUANT_LIVE_MODE", "0") == "1"

# ---------------------------------------------------------------------------
# Flow timeouts (seconds) — FSM 流程超时熔断
# ---------------------------------------------------------------------------

FLOW_TIMEOUTS = {
    "selection": 3600,   # 选股全流程 1h
    "buy": 1800,         # 单股票买入流程 30min
    "hold": 900,         # 单持仓维护流程 15min
}

# Redis key prefixes (设计文档 §6.1)
REDIS_QUEUE_PREFIX = "mq:queue:"
REDIS_DELAYED_KEY = "mq:delayed"
REDIS_DEAD_PREFIX = "mq:dead:"
REDIS_TASK_META_PREFIX = "mq:task_meta:"
REDIS_LOCK_PREFIX = "mq:lock:"
REDIS_IDEMPOTENT_PREFIX = "mq:idempotent:"
REDIS_CACHE_PREFIX = "quant:cache:"
