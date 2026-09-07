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

# 自选买入: 开盘期间每 30 分钟 (guarded by trading-session check)
BUY_SCAN_INTERVAL_MINUTES = 30

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
# 单只持仓市值占账户总资产上限 (软约束, 风控扫描用)
SINGLE_POSITION_MAX_PCT = 0.30
# 自选池观察期 (交易日), 到期自动 expired
OBSERVE_EXPIRE_TRADING_DAYS = 7
# 深度分析置信度低于该值不入池 (宁缺毋滥)
MIN_ENTRY_CONFIDENCE = 6.0

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
CONF_KICK_THRESHOLD = 0.3         # 低于该值自动移出自选池 (置信度衰竭)
CONF_BREAKOUT_THRESHOLD = 0.7     # 主线突破通道开启的最低 dyn_confidence

# 动态仓位系数 (在原 BUY_POSITION_RATIO 基础上叠加, 只影响总额度):
# pool_conf>=POS_HIGH_CONF 且 stage=BODY -> 1.2; >=POS_MID_CONF -> 1.0; 否则 0.5
# POS_MID_CONF 与 CONF_DEFAULT 对齐 (0.5): 保证旧池行/字段缺失的默认档退化为
# 原 20% 固定仓位 (§9 兼容保证), 仅被维护官衰减到默认档以下 (<0.5) 才减半。
POS_HIGH_CONF = 0.8
POS_MID_CONF = 0.5
POS_COEF_HIGH = 1.2
POS_COEF_MID = 1.0
POS_COEF_LOW = 0.5

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
IDEMPOTENT_TTL = 3600     # 调度幂等 key TTL
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

# 实施步骤 §15-1: 初始化后 global_trade_enable=0 默认关闭交易。
SYSTEM_CONFIG_SEED = {
    "global_trade_enable": ("1", "全局交易开关: 0=禁止全部下单(一键熔断), 1=允许"),
    "max_daily_orders": ("50", "单日最大委托笔数"),
    "max_order_value": ("200000", "单笔最大委托金额(元)"),
    "allowed_symbols": ("", "允许交易的股票白名单(逗号分隔, 空=不限制)"),
    "initial_cash": ("1000000", "模拟盘初始资金(元)"),
    "max_drawdown_pct": ("0.20", "账户总回撤阈值(触发冻结新建仓)"),
    "max_daily_loss_pct": ("0.03", "单日最大亏损阈值(触发冻结新建仓)"),
    "trade_frozen": ("0", "账户级风控冻结: 1=只允许卖出, 0=正常"),
}

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
