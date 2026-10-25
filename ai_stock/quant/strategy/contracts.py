"""策略台数据契约 —— 跨层传递的唯一事实源.

设计约束 (见 docs/STRATEGY_DESK_ARCHITECTURE.md §1.2):
- 纯数据: 本模块不 import db / requests / llm, 只依赖 pydantic
- 缺失是一等公民: ``Evidence.ok=False`` 承载"取不到", 不用异常穿层
- 评级与理由同一对象返回: 杜绝"评级为强、理由写着样本不足"的自相矛盾

字段口径与 PRD §5 一一对应, 改字段必须同步改 PRD 与 render.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field

# ---- 基础枚举 (Literal 而非 Enum: 落 JSON 时无需额外序列化) ----------------

Grade = Literal["A", "B", "C", "D"]
""""证据等级.
A 公告/官方名单 · B 订单/交付可验证 · C 权威媒体(未取一手) · D 推测关联.
P0 只可能产出 C/D: 公告与订单模块尚未接入 —— 不是遗漏, 是当前数据边界.
D 级不计入有效样本 (PRD §5 M1 硬规则).
"""

Driver = Literal[
    "政策", "产业", "业绩", "资金", "情绪", "地缘", "外盘", "数据", "并购重组",
]
"""驱动源. 并购重组含借壳/易主 —— 唯一不可外推为板块逻辑的驱动."""

RatingCap = Literal["强", "中", "观察", "无"]
Scope = Literal["板块级", "观察", "个股事件"]
Attribution = Literal["子概念龙头", "独立事件", "纯情绪", "无公开催化"]
FundVerdict = Literal["机构承接", "游资接力", "边拉边撤", "无法判定"]
Direction = Literal["上行", "震荡", "下行"]
Confidence = Literal["高", "中高", "中", "低"]
SnapshotType = Literal["close", "intraday"]
ClaimType = Literal["MAINLINE", "LEADER", "THEME", "NEWS"]
VerifyState = Literal["pending", "judged", "unjudged"]
Outcome = Literal["兑现", "部分", "证伪", ""]


# ---- 证据层 ---------------------------------------------------------------


class Evidence(BaseModel):
    """一条取数事实. 缺失也是一条事实, 由 ok/empty_reason 表达."""

    kind: str
    source: str
    ok: bool = True
    empty_reason: str = ""
    fetched_at: str = ""
    payload: dict = Field(default_factory=dict)


class BoardFlow(BaseModel):
    """板块当日行情 (行业或概念)."""

    code: str
    name: str
    change_pct: float = 0.0
    main_net_inflow: float = 0.0
    amount: float = 0.0


class Constituent(BaseModel):
    """板块成分股 (仅流动性靠前的若干只)."""

    code: str
    name: str
    change_pct: float = 0.0
    amount: float = 0.0
    main_net_inflow: float = 0.0


class LimitUpRow(BaseModel):
    """涨停池一行. reason_tags 是同花顺编辑归因 (C 级证据的来源)."""

    code: str
    name: str
    trade_date: str
    reason_tags: list[str] = Field(default_factory=list)


class NewsImpact(BaseModel):
    """新闻**主题级**方向 (来自新闻榜 industry_impacts, 不是标题命中).

    为什么必须区分: 标题出现"锂"字不等于锂矿看涨. 这里的 direction 是新闻榜
    评估链路给出的主题方向 (bullish/bearish/neutral), 有强度与作用域,
    可以直接进交叉验证矩阵; 标题命中只能证明"提到了", 用它凑方向会造出
    伪三方共振 —— 这正是专家团反复强调的"同一事实被重复计入"。

    strength: 1~3, 已含新闻榜的收敛处理 (弱链条会被压到 1 或直接丢弃).
    horizon: 新闻榜给的兑现窗口 (1d/3d/1w), 用于生成催化日历.
    """

    theme: str
    direction: str = "neutral"
    strength: int = 1
    horizon: str = ""
    source_count: int = 1
    base_date: str = ""
    exp_low: float = 0.0
    exp_high: float = 0.0


class MacroVars(BaseModel):
    """宏观五变量. 取不到即 None —— 不猜, 不填估计值."""

    us10y: Optional[float] = None
    real_yield: Optional[float] = None
    dxy: Optional[float] = None
    usdcnh: Optional[float] = None
    cpi_yoy: Optional[float] = None
    oil: Optional[float] = None
    dr007: Optional[float] = None
    cn10y: Optional[float] = None
    vix: Optional[float] = None
    sox: Optional[float] = None

    def missing(self) -> list[str]:
        """返回缺失变量名 (排除 cpi_yoy/oil 这类可共缺的软变量)."""
        keys = ("us10y", "dxy", "usdcnh", "dr007", "vix")
        return [k for k in keys if getattr(self, k) is None]


# ---- 研判层 ---------------------------------------------------------------


class BasketDraft(BaseModel):
    """篮子草稿 —— 由 adapters 取数拼装, 判断交给 rules.basket.synthesize."""

    parent_board: str
    name: str
    members: list[str] = Field(default_factory=list)
    change_pct: dict[str, float] = Field(default_factory=dict)
    driver: dict[str, Driver] = Field(default_factory=dict)
    evidence_level: dict[str, Grade] = Field(default_factory=dict)
    reason_tags: dict[str, list[str]] = Field(default_factory=dict)


class BasketVerdict(BaseModel):
    basket: BasketDraft
    synth_change_pct: float = 0.0
    valid_n: int = 0
    evidence_dist: dict[str, int] = Field(default_factory=dict)
    driver_consistency: float = 0.0
    rating_cap: RatingCap = "无"
    verdict: Scope = "个股事件"
    reason: str = ""

    @property
    def excluded_n(self) -> int:
        return len(self.basket.members) - self.valid_n


class LeaderTrace(BaseModel):
    symbol: str
    name: str
    boards: int = 0
    board_type: str = ""
    official_board: str = ""
    real_driver: Driver = "情绪"
    attribution: Attribution = "纯情绪"
    extrapolable: bool = False
    fund_verdict: FundVerdict = "无法判定"
    exclude_from_basket: bool = False
    reason_tags: list[str] = Field(default_factory=list)


class ShadowSeries(BaseModel):
    """自建结构概念 (影子板块) 的三日资金序列."""

    basket_name: str
    members: list[str] = Field(default_factory=list)
    d2: Optional[float] = None
    d1: Optional[float] = None
    d0: Optional[float] = None
    pattern: str = "数据缺失"
    confidence: str = "低"


class Regime(BaseModel):
    """宏观状态. growth_cap=None 的语义是"不约束", 不是"不利"."""

    code: str = "未定"
    growth_cap: Optional[RatingCap] = None
    note: str = ""
    vars: MacroVars = Field(default_factory=MacroVars)
    triggers: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class MatrixRow(BaseModel):
    """交叉验证矩阵一行 —— 以篮子(而非板块名)为粒度."""

    basket_name: str
    sector_view: str = ""
    flow_view: str = ""
    news_view: str = ""
    sector_sign: int = 0
    flow_sign: int = 0
    news_sign: int = 0
    consistency: str = "证据不足"
    pseudo_resonance: bool = False
    note: str = ""


class CrossMatrix(BaseModel):
    rows: list[MatrixRow] = Field(default_factory=list)
    resonance_n: int = 0
    divergence_n: int = 0


class Claim(BaseModel):
    """一条可证伪断言. 四态由 verify_state 承载 (PRD §7.6.1)."""

    claim_id: str
    period: str
    type: ClaimType
    target: str
    driver: Driver = "情绪"
    text: str = ""
    direction: Direction = "震荡"
    horizon: str = "T+1"
    confidence: Confidence = "中"
    falsify: str = ""
    magnitude: str = ""
    regime: str = "未定"
    crowding: str = "30-70%"
    verify_date: date
    snapshot_type: SnapshotType = "close"
    verify_state: VerifyState = "pending"


class Judgement(BaseModel):
    state: VerifyState = "pending"
    outcome: Outcome = ""
    move: Optional[float] = None
    reason: str = ""


class PriceBar(BaseModel):
    """判定用的日线. bar_date/provisional 直接来自指标层.

    ``pct_change`` 由数据层算好 (行情接口本就返回涨跌幅), 判定层不再重复算,
    避免"两处口径"这种最典型的隐性 bug.
    """

    bar_date: str
    close: float
    pct_change: float = 0.0
    provisional: bool = False


# ---- LLM 输出契约 (schema 是防截断的第一道墙) ------------------------------


class Narrative(BaseModel):
    """分析师叙述. 只允许措辞, 不允许引入新数字."""

    key_points: list[str] = Field(default_factory=list, max_length=4)
    caution: str = ""


class ArbiterVerdict(BaseModel):
    """裁决器输出. 结论 + 红队反驳, 字段数刻意压到 6 个以内."""

    headline: str = ""
    mainline: str = ""
    stage: Literal["起步", "半程", "加速", "末端", "无主线"] = "无主线"
    red_team: str = ""
    advice: list[str] = Field(default_factory=list, max_length=4)
    risks: list[str] = Field(default_factory=list, max_length=3)
