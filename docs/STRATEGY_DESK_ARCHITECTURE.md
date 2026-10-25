# 策略台（StrategyDesk）架构设计

> 版本：v1.0 ｜ 日期：2026-10-25 ｜ 上游：`docs/STRATEGY_DESK_DESIGN.md`（PRD v1.0）
> 定位：PRD 回答"做什么"，本文回答"怎么长在现有代码里、边界在哪、坏了会怎样"。

---

## 0. 架构约束（先摆事实，再谈设计）

设计的第一原则是承认现状。以下六条是这个项目已经付出的代价，架构必须顺着它们走，而不是绕开：

| # | 事实 | 架构后果 |
|---|---|---|
| C1 | 量化表**无 Alembic 链路**，`create_all` 只建新表、不给已有表补列 | 一切新增状态**新建表**，禁止 ALTER 既有表 |
| C2 | SQLite 单写入者，长事务会阻塞整个交易链路 | 事务必须短；**绝不跨网络调用持有 session** |
| C3 | MQ 是 Redis + SQLite 双后端，生产可能只跑 SQLite | 队列吞吐不可假设；每日 1 个任务，够用即可 |
| C4 | 东财 push2 已被封过；`data_health` 有熔断 | 数据缺失是**常态**，不是异常路径 |
| C5 | 消费模型是线程 + `TaskConsumer`，非 asyncio | 不引入 async，不用异步库 |
| C6 | LLM 曾因无超时把流程拖到 3600s 熔断 | 所有 LLM 调用必须带 timeout + 分批 + 上限 |

---

## 1. 架构总原则

### 1.1 三条骨架原则

**P1 · 判断是纯函数，IO 是薄壳。**
所有规则判断（篮子有效性、连板聚合、驱动分类、交叉验证、断言判定）写成无副作用的纯函数：输入 `dataclass`，输出 `dataclass`。这是"能单测、能回放 20 个交易日、能被审计"的唯一实现方式。Agent 只负责 `取数 → 调纯函数 → 落库 → report_step`。

**P2 · 不新建第二套运行时。**
复用 `QuantService` 的 consumer 装配、`BaseAgent`、`structured_invoke`、`enqueue_task`、`session_scope`、`agent_decision_log`。新增物只有：1 个队列名、1 组 handler、1 个纯函数包、7 张表、1 个渲染器。

**P3 · LLM 不允许取数。**
所有数据由规则层取好并压成结构化摘要（≤1200 token）再交给 LLM。LLM 只做两件事：把结构化事实归纳成话，把矩阵写成带红队反驳的判断。**杜绝 tool-calling 自由取数**——那会让"数据缺失"变成"编数据"。

### 1.2 分层与依赖方向

```
api / frontend
      ↑
  render.py（JSON → 三段式 markdown，纯模板）
      ↑
  service.py（strategy_flow 编排：顺序、降级、事务）
      ↑                        ↘
  agents/（取数壳 + LLM 壳）    repo.py（唯一 DB 写入口）
      ↓
  rules/（纯函数，零依赖）  ←  contracts.py（pydantic 契约）
      ↓
  ports.py（Protocol） ← adapters/（DataService 实现 / Replay 回放实现）
```

依赖只能向上：`rules` 不 import `db`、不 import `requests`、不 import `llm`。**这条线一破，回放能力就没了。**

---

## 2. 目录与文件清单

```
ai_stock/quant/strategy/            # 新增包
├── __init__.py
├── contracts.py          # 全部 pydantic 契约（唯一事实源）
├── ports.py              # EvidencePort / Repo 协议
├── adapters.py           # DataServiceAdapter / ReplayAdapter
├── repo.py               # 唯一写库入口（upsert 语义）
├── render.py             # 三段式渲染（无 LLM）
├── service.py            # strategy_flow 编排
├── rules/
│   ├── basket.py         # 聚类 + 合成涨跌幅 + 有效性裁决
│   ├── leader.py         # 连板聚合 + 驱动分类 + 可外推判定
│   ├── flow.py           # 影子板块序列 + 六态 + 风格三维
│   ├── regime.py         # 五变量 → R1–R6
│   ├── matrix.py         # 三方对齐 + 同源检查
│   └── claim.py          # 断言生成 + 四态判定
└── agents/
    ├── sector_structure.py
    ├── leader_trace.py
    ├── flow_shadow.py
    ├── macro_gate.py
    └── arbiter.py

ai_stock/quant/db_models.py         # 追加 7 个 model（不动既有表）
ai_stock/quant/config.py            # + STRATEGY_QUEUE、时间与阈值常量
ai_stock/quant/scheduler.py         # + trigger_strategy_desk() 与 job 注册
ai_stock/quant/agents/__init__.py   # + _strategy_handlers() 注册
ai_stock/dataflows/macro.py         # 新增：外盘五变量（唯一新数据源）
backend/app/api/strategy.py         # 新增 6 个端点
scripts/strategy_selfcheck.py       # 新增：S1/S2/S3 自检
tests/test_strategy_rules.py        # 28 例：四个反例 + 四态判定 + 形态
tests/test_strategy_flow.py         # 回放端到端（缺宏观也能出报告）
tests/test_strategy_isolation.py    # 依赖方向门禁（静态扫描）
```

**改动既有文件的行数预算**：`db_models` +120、`config` +12、`scheduler` +18、`agents/__init__` +8、`service` **+0**。其余全部新增。

> 实施结果：`service.py` 确实**一行都不用改** —— 消费者是按 `ALL_QUEUES` 装配的，把 `strategy` 加进常量表即自动获得独立消费者。这是"低侵入"从预算变成事实的一处实证。

**实现与原设计的偏差（已决，勿再改回）**：
1. **记分看板不落表**：分组胜率在 `StrategyRepo.score()` 实时算。落表只会制造"看板过期"这种假问题，且没有任何读者需要历史看板快照。
2. ~~M3 风格定性留空~~ → **已接线**：`rules/flow.classify_board` 用"成长端 / 防御端 / 中间端"三组板块的**均值**判风格（用合计值必然偏正，因为成长端板块数量天然更多）。任一端匹配不到 4 个板块就返回空串，前端显示"数据不足"，绝不从单边数据推出"进攻"。
3. ~~新闻方向未接入~~ → **已接线**：`CrossCheckAgent` 读新闻榜 `news_items.industry_impacts_json` 的**主题级 direction/strength**（不是标题命中），交给 `rules/news.sign` 转符号。仍有两条纪律：
   - 单条 `strength=1` 不构成方向（阈值 `MIN_STRENGTH=2`），否则每天都能凑出"利多"；
   - 新闻主题与涨停归因**同名**时，该条不计入独立事实 —— 那只是把归因复述一遍，算两次就构成伪共振。

**P1/P2 补齐后的职责变化**：
- 矩阵从裁决器里搬进独立的 `CrossCheckAgent`（M5），裁决器只读结论、不参与取数与判定 —— 否则"矩阵怎么算的"无法回放。
- 龙虎榜席位只查 ≥3 连板的高标（通常 <10 只）。全市场逐股查会触发东财限流，而限流的代价是整块数据一起缺，远大于"少一条席位证据"。
- 机构净卖出**不产出 `False`**：`rules/leader.judge_fund` 里 `False` 意味着"游资接力"，而机构出货是另一个结论。让它回落到 D0/D1 判定，别用"两个不同事实共用一个布尔值"。

---

## 3. 契约层（contracts.py）

一切跨层传递都过 pydantic，不用裸 dict。核心几个：

```python
from __future__ import annotations
from datetime import date
from enum import Enum
from typing import Literal, Sequence
from pydantic import BaseModel, Field

Grade = Literal["A", "B", "C", "D"]
Verdict = Literal["板块级", "观察", "个股事件"]
Driver = Literal["政策", "产业", "业绩", "资金", "情绪", "地缘", "外盘", "数据", "并购重组"]
Attribution = Literal["子概念龙头", "独立事件", "纯情绪", "无公开催化"]


class Evidence(BaseModel):
    """一条取数事实。缺失也是一条事实（ok=False），不走异常。"""
    kind: str                      # board_flow / limit_up / fund_flow / concept / news / macro
    source: str                    # eastmoney_push2 / ths_hsgt / baidu_pae / ...
    ok: bool = True
    empty_reason: str = ""         # 熔断 / 超时 / 停牌 / 无数据
    fetched_at: str = ""
    payload: dict = Field(default_factory=dict)


class BasketDraft(BaseModel):
    parent_board: str
    name: str
    members: list[str]
    change_pct: dict[str, float]           # symbol → 当日涨跌幅
    driver: dict[str, Driver]              # symbol → 驱动
    evidence_level: dict[str, Grade]       # symbol → A/B/C/D
    reason_tags: dict[str, list[str]] = Field(default_factory=dict)


class BasketVerdict(BaseModel):
    basket: BasketDraft
    synth_change_pct: float
    valid_n: int                            # = A/B/C 计数
    evidence_dist: dict[str, int]
    driver_consistency: float               # 主驱动占比
    rating_cap: Literal["强", "中", "观察", "无"]
    verdict: Verdict
    reason: str


class LeaderTrace(BaseModel):
    symbol: str
    name: str
    boards: int
    board_type: str                         # 首板/二连/三连/断板
    official_board: str
    real_driver: Driver
    attribution: Attribution
    extrapolable: bool                      # 能否外推为板块逻辑
    fund_verdict: Literal["机构承接", "游资接力", "边拉边撤", "无法判定"]
    exclude_from_basket: bool


class Claim(BaseModel):
    """一条可证伪断言。四态由 verify_state 承载，不做二态。"""
    claim_id: str                           # P7-M1
    period: str                             # 2026-10-27-P7
    type: Literal["MAINLINE", "LEADER", "THEME", "NEWS"]
    target: str
    driver: Driver
    text: str
    direction: Literal["上行", "震荡", "下行"]
    horizon: str
    confidence: Literal["高", "中高", "中", "低"]
    falsify: str
    magnitude: str                          # -5%~+8%
    regime: str
    crowding: Literal["<30%", "30-70%", ">70%"]
    verify_date: date
    snapshot_type: Literal["close", "intraday"] = "close"
    verify_state: Literal["pending", "judged", "unjudged"] = "pending"
```

设计要点：
- **`Evidence.ok=False` 是一等公民**。C4 说数据缺失是常态——把它建模成字段，而不是让异常穿三层栈。
- **`BasketVerdict` 自带否决理由**。评级和理由同一个对象返回，杜绝"评级是强、理由写着样本不足"的自相矛盾。
- **`Claim.snapshot_type` 默认 `close`**，`intraday` 必须在构造时显式传入，从类型层面拦住污染。

---

## 4. 规则层（rules/）—— 全部纯函数

### 4.1 basket.py（最核心，对应 G1）

```python
MIN_VALID_FOR_BOARD = 5      # 有证据样本 <5 不得给板块级评级
STRONG_VALID = 8             # ≥8 才可"强"

EXCLUDE_DRIVERS = frozenset({"并购重组"})   # 借壳/易主归入此项


def synthesize(draft: BasketDraft) -> BasketVerdict:
    """合成涨跌幅 + 证据分布 + 驱动一致性 + 评级上限。

    纯函数：不读库、不发网络。同一 draft 永远得到同一 verdict（可回放的关键）。
    """
    kept = [s for s, d in draft.driver.items() if d not in EXCLUDE_DRIVERS]
    valid = [s for s in kept if draft.evidence_level.get(s, "D") in ("A", "B", "C")]

    dist: dict[str, int] = {}
    for s in kept:
        g = draft.evidence_level.get(s, "D")
        dist[g] = dist.get(g, 0) + 1

    synth = round(sum(draft.change_pct[s] for s in kept) / max(len(kept), 1), 2)
    top = max(dist, key=dist.get) if dist else "D"

    return BasketVerdict(
        basket=draft, synth_change_pct=synth, valid_n=len(valid),
        evidence_dist=dist,
        driver_consistency=round(dist.get(top, 0) / max(sum(dist.values()), 1), 2),
        rating_cap=_cap(len(valid)), verdict=_verdict(len(valid)),
        reason=_reason(len(valid), len(draft.members) - len(kept)),
    )


def _cap(valid_n: int) -> Literal["强", "中", "观察", "无"]:
    if valid_n >= STRONG_VALID:
        return "强"
    if valid_n >= MIN_VALID_FOR_BOARD:
        return "中"
    if valid_n >= 2:
        return "观察"
    return "无"          # 1 只 = 个股事件，无板块评级


def _verdict(valid_n: int) -> Verdict:
    if valid_n >= MIN_VALID_FOR_BOARD:
        return "板块级"
    return "观察" if valid_n >= 2 else "个股事件"
```

**为什么这样写**：`_cap` / `_verdict` / `_reason` 是三个只依赖一个整数的纯映射，可以逐条参数化测试（valid_n = 0,1,2,4,5,7,8,12 全覆盖）。没有 `if` 嵌在 IO 里，也没有"顺便查一下库"的诱惑。

### 4.2 leader.py（对应 G2）

```python
def aggregate_boards(rows: Sequence[LimitUpRow], lookback: int = 5) -> dict[str, int]:
    """连续涨停交易日计数（按交易日序列，不按自然日）。"""
    ...

def classify(real_driver: Driver, has_public_catalyst: bool) -> tuple[Attribution, bool]:
    """返回 (归属类型, 是否可外推为板块逻辑)。

    并购重组/借壳/易主 → 独立事件，不可外推（PRD 硬约束，此处是唯一实现点）。
    """
    if real_driver == "并购重组":
        return "独立事件", False
    if not has_public_catalyst:
        return "无公开催化", False
    return "子概念龙头", True
```

**关键**：`classify` 是个三行函数，但它是"妖股不许写成板块启动"这条纪律的**唯一实现点**。全系统其他位置不得再判一次——重复判定必然分叉。

### 4.3 claim.py（对应隐患 1/2/3）

```python
class Judgement(BaseModel):
    state: Literal["pending", "judged", "unjudged"]
    outcome: Literal["兑现", "部分", "证伪", ""] = ""
    move: float | None = None
    reason: str = ""


def judge(claim: Claim, bars: Bars | None, asof: date) -> Judgement:
    """四态判定。bar_date 不匹配或 provisional=True 一律退回 pending。"""
    if asof < claim.verify_date:
        return Judgement(state="pending")

    if bars is None or not bars.complete_on(claim.verify_date):
        return Judgement(state="unjudged", reason="日线缺失/停牌/接口失败")

    move = bars.pct_change(claim.verify_date)
    return Judgement(state="judged", outcome=_outcome(claim, move), move=move)
```

`Bars.complete_on(d)` 内校验 `bar_date == d and not provisional`——**两个字段指标层本来就有**（`get_stock_indicators` 的 `bar_date` / `provisional`），这不是新造轮子。

### 4.4 regime.py —— 唯一允许"缺数据就不下结论"的模块

```python
def decide(v: MacroVars) -> Regime:
    missing = v.missing()
    if len(missing) >= 2:
        return Regime(code="未定", growth_cap=None,
                      note=f"缺失 {len(missing)} 个变量，不下调评级上限")
    ...
```

`growth_cap=None` 的语义是"不约束"，而不是"约束为低"——**"不知道"和"不利"必须能区分**，否则一次接口故障就会让全市场成长板块降级。

---

## 5. 端口层（ports.py + adapters.py）

```python
class EvidencePort(Protocol):
    def board_flow(self, d: str) -> list[BoardFlow]: ...
    def limit_up(self, d: str, days: int = 5) -> list[LimitUpRow]: ...
    def fund_flow(self, symbols: Sequence[str], days: int = 20) -> dict[str, list[float]]: ...
    def concepts(self, symbol: str) -> list[str]: ...
    def news(self, d: str) -> list[NewsItem]: ...
    def macro(self) -> MacroVars: ...
```

两个实现：

| 实现 | 用途 | 行为 |
|---|---|---|
| `DataServiceAdapter` | 生产 | 调 `DataService`；异常 → 空列表 + `Evidence(ok=False, empty_reason=...)` |
| `ReplayAdapter` | 测试/回放 | 读 `strategy_evidence` 历史快照，**零网络** |

**这是"回放最近 20 个交易日"这条验收标准的实现基础**。没有这层抽象，验收只能靠人肉跑生产。

---

## 6. Agent 层（薄壳，每个 ≤120 行）

统一形状，四个 agent 长得一样：

```python
class SectorStructureAgent(BaseAgent):
    agent_name = "strategy_sector_structure"

    def run(self, ctx: RunContext) -> StepResult:
        boards = self.port.board_flow(ctx.trade_date)[:5]
        drafts = [cluster(b, self.port) for b in boards]      # 取数
        verdicts = [synthesize(d) for d in drafts]            # 纯函数
        self.repo.upsert_baskets(ctx.trade_date, verdicts)     # 落库
        return StepResult(ok=bool(verdicts), degraded=not verdicts,
                          summary=f"{len(verdicts)} baskets")
```

`cluster()` 是唯一有 IO 倾向的地方（要拉成分股、概念归属），但它只做"取数 + 拼 draft"，判断全在 `synthesize` 里。

### 编排（service.py）

```python
STEPS = ("sector_structure", "leader_trace", "flow_shadow", "macro_gate", "arbiter", "persist")

def run_strategy_flow(trade_date: str, snapshot_type: str = "close") -> dict:
    ctx = RunContext(trade_date=trade_date, snapshot_type=snapshot_type,
                     port=build_port(), repo=StrategyRepo())
    for name in STEPS:
        result = HANDLERS[name](ctx)          # 单步失败不终止
        ctx.absorb(name, result)              # 失败 → 记降级，继续
    return ctx.finish()                       # 单事务落报告
```

**为什么是顺序而非并行**：步骤间有数据依赖（M2 要用 M1 的 basket，M3 要用 M1+M2，M5 用前三者）。四个 agent 里只有 `flow_shadow` 与 `macro_gate` 可并行，但省下的 3 秒不值得引入线程池的调试成本——**每日 1 次的任务，优先可读性**。

---

## 7. 持久化（repo.py）—— 唯一写入口

```python
class StrategyRepo:
    def save_run(self, ctx: RunContext) -> None:
        """单事务提交：报告 + 篮子 + 高标 + 影子 + regime + 断言 + 证据。"""
        with closing(session_scope()) as s:
            self._upsert_report(s, ctx)        # 可覆盖（重跑）
            self._insert_claims(s, ctx)        # 不可覆盖（见下）
            s.commit()
```

三个必须做对的细节：

1. **报告可重跑覆盖，断言不可**。报告用 `ON CONFLICT(trade_date, snapshot_type) DO UPDATE`；断言用 `ON CONFLICT(period, type, seq) DO NOTHING`。否则重跑一次，胜率统计就被重复计数污染一次。
2. **不跨网络持有 session**（C2）。取数在 `agents` 里完成，`session` 只在 `save_run` 内打开，事务时长目标 <200ms。
3. **intraday 断言直接拒绝入库**：在 `_insert_claims` 里断言 `snapshot_type == "close"`，违反抛 `ValueError`。用异常而非注释，是因为这条规则被违反的代价是"统计静默失效"。

**表设计与 C1**：7 张表全部继承 `QuantBase`，靠 `init_quant_db()` 的 `create_all` 建。**上线动作只有一条：重启服务**（`init_quant_db` 在 lifespan 里跑）。这点必须在运维文档写清——否则会有人去手工 DDL。

---

## 8. 时序、并发与一致性

### 8.1 进程内时序（单次运行）

```
15:05 enqueue(strategy_2026-10-27_close)        ← scheduler，幂等键=trade_date+snapshot
15:05 consumer pop → run_strategy_flow
      ├ sector_structure  ~8s   取数 5 板块 + 概念归属
      ├ leader_trace      ~5s
      ├ flow_shadow       ~3s
      ├ macro_gate        ~2s   yfinance 6 个 ticker（失败即 missing）
      ├ arbiter           ~25s  1 次 deep LLM
      └ persist           ~0.2s 单事务
15:06 done → report_step
```

### 8.2 并发控制

| 风险 | 处置 |
|---|---|
| 15:05 与 15:35 `daily_report` 抢 config 写 | 策略台**只读** config；唯一写点是周频系数回灌（周一 07:00 前，独立任务） |
| 同一交易日被调度与手动各跑一次 | 幂等键 `strategy_{date}_{snapshot}`；`get_running_flows("strategy")` 二次挡 |
| SQLite 写锁阻塞交易 | 单事务 + 事务内无网络；写窗口定在 15:05–15:10（交易链路空闲） |
| 18:00 selection 读到半个篮子 | 报告单事务提交，`effective_from` 标记；读侧只取 `effective_from <= now` 的完整报告 |

### 8.3 与交易链路的隔离（PRD §7.5 的代码落点）

```
读侧契约：selection 只读 strategy_report（不读 basket 明细、不读 prediction）
写侧契约：strategy 包内不 import broker / risk_control / mq.enqueue_task(BUY_QUEUE)
```

第二条是**可由静态检查强制的**：在 `tests/test_strategy_isolation.py` 里扫描 `ai_stock/quant/strategy/**` 的 import，命中 `broker|risk_control|agents.buy` 即失败。把"不许碰交易"从口头纪律变成 CI 门禁。

---

## 9. LLM 层设计（成本与可控性）

| 调用 | 模型 | 输入 | 输出 schema | 失败降级 |
|---|---|---|---|---|
| sector 分析师 | quick | basket JSON（≤1000 token） | `SectorNarrative` | 用模板拼，缺"归因措辞" |
| flow 分析师 | quick | shadow 序列 JSON | `FlowNarrative` | 同上 |
| news 分析师 | quick | 主题榜前 20 条 | `NewsNarrative` | 同上 |
| 裁决器 | deep | matrix + 三段叙述（≤3000 token） | `Verdict`（含 claims[]、red_team） | **降级为规则报告**：不生成红队段落，`degraded=true` |

四条硬规则：
1. 全部走 `structured_invoke(llm, Schema, prompt, default=None, deep=...)`，不自己解析 JSON。
2. `PROMPT_VERSION: int = 1` 常量落库，改提示词必须 +1（沿用项目既有约定）。
3. 输入摘要硬截断 `_clip(text, N)`，输出 schema 字段数 ≤12——**schema 是防截断的第一道墙**（项目已有 12 条批被截断的教训）。
4. 单日 LLM 调用上限 4 次，超限直接 `degraded=True`。

---

## 10. 降级矩阵（实事求是：每步坏了会怎样）

| 步骤 | 失败表现 | 系统行为 | 用户可见 |
|---|---|---|---|
| 板块资金流 | 熔断/空 | basket 为空 | Tab1 显示"结构拆解不可用（数据缺失）" |
| 涨停池 | 空 | 无高标卡 | 「今日无高标或数据缺失」+ 不猜测 |
| 个股资金 | 部分失败 | 该股 `fund_verdict=无法判定` | 高标卡标灰 |
| 概念归属（百度 PAE） | 全失败 | 无法聚类 → basket 退化为"板块成分股等权" | 标注"粗口径（未细分）" |
| 宏观五变量 | 缺 ≥2 | `regime=未定`，**不下调任何评级上限** | 抬头红字"宏观数据缺失，本期不做评级约束" |
| 裁决 LLM | 超时/解析失败 | 规则报告（无红队、无措辞） | `degraded=true` 徽标 |
| 全链失败 | 任何异常 | 仍落一条空报告 + `degraded=true` | "本期生成失败，已留痕" |

最后一行是关键：**空报告也比没有记录好**——否则记分体系会出现"某些交易日凭空消失"，而那正是系统性偏差最容易藏身的地方。

---

## 11. 可观测性与自检

- **决策日志**：每个 agent 一行 `agent_decision_log`（复用 `BaseAgent.log`），含 `degraded`、耗时、evidence 缺失数。
- **自检脚本** `scripts/strategy_selfcheck.py`：跑 PRD §7.6 的 S1/S2/S3，退出码非 0 即 CI 红。
  - S1：近 20 交易日**每日都有** trade_log（防红线 1 失效）
  - S2：`count(snapshot_type=close) == count(prediction)`（防盘中污染）
  - S3：单 driver `unjudged` 率 ≤30%（防数据质量拖垮权重）
- **指标**：`strategy_run_ms`、`evidence_missing_count`、`claims_created`、`unjudged_rate`、`degraded_steps`。

---

## 12. 测试策略

| 层 | 方式 | 覆盖目标 |
|---|---|---|
| `rules/*` | 纯单测，golden case 参数化 | 80%+；`valid_n` 边界全覆盖 |
| 四个已知反例 | 固定输入 → 断言 `verdict != 板块级` | 家电/锂矿/航运/CPO 必须降级 |
| `ReplayAdapter` | 回放 20 个交易日全链路 | 不崩、不空、三段式不缺项 |
| 隔离测试 | 扫 import | 不允许依赖 broker / risk_control |
| 契约测试 | 校验 `strategy_report.sections_json` 三段齐全 | 防"退回多节结构" |

**四个反例是回归基线**：它们是团队连续复发三次的错误，必须有自动化测试守着，而不是靠人记住。

---

## 13. 落地顺序与工作量（实事求是版）

| 阶段 | 交付 | 代码量 | 风险 |
|---|---|---|---|
| P0-a | `contracts` + `rules/basket` + `rules/leader` + 单测 | ~700 行 | 低（纯函数，无依赖） |
| P0-b | `ports` + `adapters` + 2 个 agent + `repo` + 表 | ~600 行 | 中（概念归属接口稳定性） |
| P0-c | `service` + `render` + API + 一页前端 | ~700 行 | 中（前端工作量常被低估） |
| P1 | `rules/regime` + `rules/matrix` + `rules/claim` + 记分 | ~800 行 | 中（外盘数据源接入） |
| P2 | `rules/flow` + 系数回灌 + 事件日历 | ~600 行 | **高**（唯一会动交易链路的一段，必须有开关兜底） |

**P1/P2 已全部交付**（详见 §2 偏差节与 §15 回灌节）：
- P1：新闻方向（`rules/news` + `CrossCheckAgent`）、龙虎榜席位（`adapters.institutional`）、防御端风格口径（`rules/flow.classify_board`）。
- P2：`strategy/feedback.py` 系数回灌（周一 06:30 周任务 → `quant_system_config` → `selection` 作乘子）、催化窗口（`news.catalyst` 写入报告的 `calendar` 字段，替代原占位符）。

总量约 4500 行（含补齐部分），纯函数与测试仍占 60%。

---

## 15. 权重回灌（P2，唯一碰交易链路的一段）

链路：`strategy_prediction` → `repo.score().driver_quality` → `feedback.compute_coefs`
→ `quant_system_config['strategy_driver_coef']` →（开关打开时）`selection` 的 `priority` 乘子。

| 闸门 | 值 | 为什么必须是这个 |
|---|---|---|
| 样本门槛 | `judged ≥ 30` | n=1 的胜率只能是 0% 或 100%，没有统计意义 |
| 数据质量 | `unjudged_rate ≤ 30%` | 一次东财封禁就能把整周断言打成 unjudged，那天绝不该调权 |
| 限幅 | `0.8 ~ 1.2` | 乘子只能微调排序；放大到能翻转排序时就等于让分析结论接管选股 |
| 频率 | 周一 06:30 | 按日调权 = 把追涨杀跌自动化，且会污染自己的样本 |
| 开关 | `strategy_reproduce_coef_enable`，默认 `0` | 先观察系数是否稳定，再决定是否让它影响排序 |

三条不做：**不加新项**（只乘、不加分）、**不改门槛**（`MIN_ENTRY_CONFIDENCE` 等一概不动）、
**不猜 driver**（行业名匹配不上篮子就不调权 —— 猜错的乘子比不调更糟，且事后无法归因）。

---

## 14. 明确不做（架构级取舍）

| 不做 | 理由 |
|---|---|
| 不为新表建 ORM 关系/外键 | SQLite 外键约束默认关闭，`create_all` 也不会帮你维护；关系用查询表达更诚实 |
| 不引入 asyncio / httpx 并发 | 与线程消费模型混用会打架（C5）；每日 1 次任务不需要并发 |
| 不用 LLM 做分类（驱动/证据等级） | 规则可测、可解释、零成本；LLM 分类无法回归验证 |
| 不做增量计算 | 单日数据量小（几百只涨停、~20 板块），全量重算 <10s，增量的复杂度换不回收益 |
| 不做实时流 | T+1 节奏下盘中实时无决策价值（PRD 已定） |
| 不动 `pre_trade_check` | 它是交易链路第一道防线，任何分析结论都无权改写布尔闸 |

---

## 15. 一页纸总结

**架构的本质是一次"边界划分"**：
把"判断"从"取数"和"交易"里切出来，做成可单测、可回放的纯函数；把 "缺失" 做成一等公民而不是异常；把 "结论" 做成可回填的断言而不是散文；把 "对交易的影响" 压缩成一条只读的 `effective_from` 契约和一个 0.8–1.2 的乘子。

这样做的收益不是"代码更漂亮"，而是三条可验证的性质：
1. **可回放**——`ReplayAdapter` 能把任意历史日期跑一遍，四个反例自动守门；
2. **可降级**——任一数据源挂掉，报告仍能生成，且用户看得见哪一块是缺的；
3. **可隔离**——静态检查就能证明它不会碰订单。
