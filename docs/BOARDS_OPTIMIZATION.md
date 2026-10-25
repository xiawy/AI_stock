# 三榜优化设计（新闻榜 / 行业榜 / 热股榜）

> 视角：产品。依据：当前仓库代码（引用到文件行为止）。
> 结论先行：**三榜的问题不在"信息不够"，在三个更根本的地方——看到的不是变化、三榜不是同一套口径、看到的东西不可验证。**
> 所以优化主线是「口径对齐 → 差异视图 → 证据链 → 成绩单」，不是「再加字段、再加数据源」。

---

## 0. 一句话诊断

| 现状 | 用户真实想问 | 差距 |
|---|---|---|
| 三个榜各自刷新、各自排序 | 今天**变了什么** | 只有快照，没有变化维 |
| 新闻榜说"CPO 利好"、行业榜把 CPO 排第 7、热股榜里有 3 只 CPO 票 | 这**是不是同一件事** | 三榜互不引用，且行业→新闻用的是另一套归因算法 |
| 每条都写"预期 +3~6%"、"置信 0.75" | 上次这么说的**兑现了吗** | 精度账本已存在，前端零展示 |

一句话：**现在三个榜是"三张报表"，产品要的是"一条主线 + 一份成绩单"。**

---

## 1. 代码事实盘点（优化的起点）

| | 新闻榜 | 行业榜 | 热股榜 |
|---|---|---|---|
| 产出者 | `macro_event` → `save_news_board` | `selection._persist_industry_board` | `radar.ConfidenceMaintainAgent` 维护池 |
| 重算时点 | 选股 3 槽：07:00 / 12:00 / 18:00 | 同左 | 同左（仅维护 `dyn_confidence`） |
| 排序依据 | 配额排序（政策≤8、行业≥6、公司≥3、资金≥2、海外≥1）+ `importance×新鲜度×源数` | `heat_score = priority`＝六项加权（供需失衡×0.40 + 阶段×0.30 + 上游×0.15 + 涨停潮×0.15 + 周期 + 壁垒） | `confidence` 降序（**入池时的静态 LLM 分，0-10，之后不再变**） |
| 存储 | `news_items` + `news_impact_tracking` | `quant_industry_board`（按日期**覆盖**写） | `quant_stock_pool_optional`（**无快照**，只存当前状态） |
| 前端 | `ImpactRanking.vue` 单表格 | `IndustryRanking.vue` 三栏联动 | `StockRecommendation.vue` 卡片列表 + `StockCard` |

策略台（`strategy_report` / `strategy_prediction`）已经是第四个页面，**但它与三榜没有任何数据引用**。

---

## 2. 六个必须承认的问题（附代码证据）

### 2.1 新闻榜

**① 排序为"版面多样性"服务，不为"可交易"服务**
`news_board.py:14-21` 的配额制度写得很清楚：它的设计目标是消灭"677 条里 100% 是政策"这个旧毛病。目标达成了，但代价是——政策密集日 20 条里可能 **0 条**能落到 L3 主题。前端已经把这个窘境暴露成一个指标 `可交易信号 N/20`（`ImpactRanking.vue:25-30`），却没有任何"按可交易性重排/分区"的手段。**指标承认了问题，交互没有解决它。**

**② 可信度不可见，且降级是静默的**
`assess_source`（`llm` / `rule`）落库了，前端从不展示。而两类的置信度差一个数量级（规则 0.35，LLM 通常 0.7+）。项目记录里有过"整批静默降级为规则"的事故（快照 981，20 条全 0.35），当时只能靠人去库里查。
更糟的是前端在用**置信度数值倒猜来源**：`confidence <= 0.31` 判为"历史回填"（`ImpactRanking.vue:293-295`）。这是拿数据猜元数据的典型症状——字段就在库里，只是没透出。

**③ 全站唯一的精度账本，藏在一个脚本里**
`news_impact_tracking` 存了 `base_date / direction / horizon / exp_low / exp_high`，`scripts/news_impact_review.py` 会按 1d/3d/1w 回填 `actual_return / direction_hit / range_hit`，并且口径是**主题成分股等权**（不是市值加权，理由写在脚本注释里，很对）。
这是整个系统唯一能回答"我说涨、到底涨了没"的客观数据。**前端零展示。** 一个产品的核心资产被放在需要 SSH 上去跑脚本才能看到的地方。

**④ 同一事件被拆成多行，榜单密度被稀释**
`cluster_id` / `source_count` 都有值（`news_impact.py:253-304`），前端只显示一个 `×3`，不折叠。20 条里出现 6 条同源转发时，用户看到的是"6 条新闻"，实际是"1 件事"。

**⑤ 同日多版本不可见**
一天 3 份快照，但 `period` 只有 AM/PM（`news_board.py:166`）——12:00 与 18:00 都是 PM。前端只取最新一份，用户既看不到"今天这条从第 3 名升到第 1 名"，也分不清两份 PM 快照的区别。

**⑥ 首页文案在描述一个已经不存在的机制**
`Home.vue:36-42` 写"4-Agent 并行打分、供需量化、多空辩论三重过滤"。而 `news_board.py:1-11` 开头第一句就是"新闻榜不再走独立的重型评估流程（多 Agent 评分/辩论）"。**文案比代码落后了一个版本**，这是最便宜的信任损耗。

### 2.2 行业榜

**① 六个因子的计算过程，落库时被扔掉了 5 个**
`selection.py:331-354` 算了 `imbalance_score / stage / stage_corrected / upstream_bonus / wave_bonus / expansion_cycle / entry_barrier`，然后合成为一个 `priority`。
`selection.py:1326` 落库时只写 `heat_score = priority`。**用户看到一个 7.3 分，无法知道它是因为供需强（7 分）还是因为只是被上游点名（+0.15）。** 六项拆解全在内存里，一秒后消失。

**② 没有"变化"这一维**
`save_industry_board` 按日期覆盖写（`db_ops.py:300-332`），历史可按日期查。所以**跨日对比一次查询就能算**，但前端只有绝对排名：没有 Δrank、没有连续在榜天数、没有"今日新进"。榜单产品的核心价值是变化，这里恰好没有。

**③ 涨幅与资金只有"当日一格"**
`change_pct` / `main_net_inflow` 都是当日值。于是"今天流入但昨天流出"和"连续三天流入"在榜上长得一模一样——而这恰恰是判断承接力最关键的区别。

**④ 龙头股标签有一个永远不会命中的分支**
`selection.py:1290-1296` 只产出 `leader_label` 为 `"精选"` 或 `""`。前端 `IndustryRanking.vue:88-91` 却在判 `=== '领涨'` 给它红色标签。**`'领涨'` 这个值永远不会出现。** 于是"哪个是当日最强"这个最该有的信息，视觉上根本不存在。
另外 `"精选"` 的真实含义是"已入自选池"，用户看不懂，等于没标。

**⑤ 行业→新闻用的是另一套归因算法（跨页矛盾）**
`db_ops.py:176-220`：行业榜点"相关新闻"时，用 `event_industry_kb.match_industries` 从标题正文**现算**行业，再与行业名模糊匹配。
而新闻榜用的是一次评估里的 `industry_impacts`（LLM 或规则）。
**两条独立路径** → 同一条新闻，在新闻榜里属于 A 行业，在行业榜的弹窗里可能属于 B 行业。用户一旦发现，两个榜的可信度一起归零。

**⑥ 上游传导行与正式排名混排**
`_transmission_rows` 会附加超出 Top10 的行（`MAX_TRANSMISSION_BOARDS`），`rank` 编号连续，只有一个小 tag 区分。用户会以为它们同权。

### 2.3 热股榜

**① 排序字段用错了（三榜唯一的"展示口径 ≠ 决策口径"）**
`pipeline_service.py:103-107`：按 `confidence`（0-10，入池时 LLM 打的静态分，**之后永不变化**）降序。
而系统真正的决策口径是 `dyn_confidence`（0-1，每轮由 `ConfidenceMaintainAgent` 增强/衰减）：
- `buy.py:212` 按 `dyn_confidence` 给买入候选排队；
- `radar.py:95-107` 中 `dyn < 0.25` 直接移出池（置信度衰竭），`is_tail_phase` 打 `stage_batch='TAIL'` 供买入拦截。

**后果**：用户看到的第 1 名，可能正是系统这一轮准备踢掉的票。这不是"展示不够好"，是榜单在给出与系统相反的信号。**这也是三榜里唯一会误导人的硬缺陷。**

**② `stage_batch` 有值但不展示**
HEAD / BODY / TAIL 三级，buy 链路对 TAIL 明确拦截，榜上一视同仁。用户不知道自己在看鱼尾。

**③ 没有"入池后表现"，也没有到期预警**
`add_time`、`observe_expire`、`kicked_reason` 全在表里，榜上一个都不显示。于是这个榜无法自证"我选得准不准"，也无法提醒"这只还剩 2 天到期"。

**④ 历史视图不可复现**
`get_optional_pool_as_of`（`pipeline_service.py:116-131`）是按 `add_time` / `remove_time` **倒推存在性**，但分数取的是**当前值**。
所以查"昨天的热股榜"，看到的是"昨天的成分 + 今天的分数 + 今天的阶段"。**历史会随时间变化**，这不是快照，是模拟。

**⑤ 同题材不折叠**
池里 15 只有 8 只同一个行业时，榜就是 8 行同义复述，用户得不到"分散"这个信息。

---

## 3. 优化方案：四层，逐层可独立上线

### 第一层 · 口径对齐（P0，约 1 天，零新表零新数据）

目标：**不再有"看不见的降级"和"自相矛盾的信号"。** 这一层全部是修正，不是新功能。

| # | 动作 | 落点 | 为什么必须先做 |
|---|---|---|---|
| 1 | 热股榜排序改 `dyn_confidence`，同时并列展示 `confidence`（标注"入池分"） | `pipeline_service.py` 两个 `sorted` | 展示与决策对齐；两个分并列还能直接暴露"入池看好、现在衰减"这个最有用的信号。**纯展示改动，不影响下单** |
| 2 | 行业榜龙头标签落真值：`领涨` = 当日涨幅第一且跑赢板块；`精选` = 已入池 | `selection.py:1290` | 让前端那条死分支活过来，且两个标签含义自解释 |
| 3 | 新闻榜加来源徽章（LLM / 规则），去掉用 `confidence<=0.31` 猜来源 | `ImpactRanking.vue` | 降级必须可见，这是信任底线 |
| 4 | 新闻榜按 `cluster_id` 折叠同事件，一行代表一件事，展开看多源 | `ImpactRanking.vue` | 20 条的信息密度立刻翻倍 |
| 5 | 首页文案改成与代码一致的描述 | `Home.vue:36-42` | 零成本 |

**验收**：① 热股榜榜首 = 系统买入候选队列首位；② 任意一条新闻都能一句话说清"这是模型判的还是规则兜的"；③ 全站不再出现"标称能力 > 实际能力"的文案。

### 第二层 · 变化视图（P1，约 4 天，1 张新表）

目标：**每个榜都能回答"今天什么变了"。**

- **行业榜 / 新闻榜**：不需要新表。行业榜本来按日期存，取昨日 + 今日两次查询即可算 `Δrank / 新进 / 连榜 N 天`；新闻榜取相邻两份快照即可算 `新增 / 上升 / 下降 / 掉榜`。
- **热股榜**：需要一张日快照表 `quant_pool_snapshot(trade_date, symbol, dyn_confidence, stage_batch, status, close)`，收盘后写一次（约 15 行/天，一年 4000 行）。**这是本方案唯一新增的写入**，其余全部是读。
  理由：现在的历史是倒推的，不可复现；有了这张表，"入池后 5 日胜率"才成立。
- **新接口 3 个**：`/api/industry/diff`、`/api/impact/diff`、`/api/recommendation/diff`。
- **前端**：每榜加"变化"列/区，并支持"按变化幅度排序"切换。

**验收**：打开任一榜，3 秒内能说出"今天新进了什么、掉了什么、名次变动最大的是谁"。

### 第三层 · 证据链（P1.5，约 3 天，1 个聚合接口 + 1 个组件）

目标：**同一个主题，在三个页面上指向同一批新闻、同一批股票。**

- 聚合接口 `/api/board/chain?theme=CPO` 返回 `{news[], industry, leaders[], pool[]}`（跨两库一次查）。
- 组件 `EvidenceChain.vue`：`新闻（事件）→ 主题 → 行业 → 龙头 → 池内标的`，四段可点，三榜复用。
- **顺手统一归因**：行业→新闻改为优先读 `industry_impacts`（与新闻榜同源），`match_industries` 降为兜底。这解决 2.2⑤ 的跨页矛盾，且是"减法"——删掉一条独立路径。

**验收**：给定一个主题，三个页面上的"相关新闻"是同一批（逐条 id 比对）；差异为 0。

### 第四层 · 成绩单（P2，约 3 天，纯读）

目标：**每一行都能点开"上次这么说，结果如何"。** 这是把策略台的记账思想下沉到三榜。

| 榜 | 成绩单口径 | 数据 |
|---|---|---|
| 新闻榜 | 近 30 日方向命中率、区间命中率，按 `event_type` / 主题分组 | `news_impact_tracking`（已有，`news_impact_review.py` 已回填） |
| 行业榜 | 上周 Top5 的 5 日**等权**收益 vs 大盘超额 | 日线（已有） |
| 热股榜 | 入池 5 日胜率、平均收益、按 `stage_batch` 分组 | 第二层的新快照表 + 日线 |

三条纪律（防止成绩单本身变成噪音）：
1. **分母只算已判定样本**，数据缺失记「无法判定」并排除——沿用策略台的口径，不把接口故障算成判断失误；
2. 样本 < 30 只显示"样本不足"，不出胜率数字；
3. 成绩单**只读**，不回写任何阈值——一旦让它自动调参，就会重演"单一日频信号驱动全局参数"的死锁。

---

## 4. 一个更激进但可能更对的选项：新闻榜的单位该换成"主题"

现在的新闻榜以**新闻条目**为单位。但用户做决策以**主题**为单位，前端 `可交易信号 N/20` 这个指标其实已经在暗示这件事。

两条路线：

| | 方案 A（分区 + 折叠） | 方案 B（主题视图为主） |
|---|---|---|
| 形态 | 上半区「可交易信号」，下半区折叠「宏观背景」 | 一行 = 一个主题：独立催化条数、方向、预期幅度、涉及行业、池内标的 |
| 优点 | 改动小，保留单条新闻的时序感 | 直接对应决策单位；天然解决同事件重复；天然是证据链的入口 |
| 缺点 | "0 可交易"的日子依然是满屏空列 | 会失去"某条新闻几点发的"这种细节感 |
| 判定 | 可交易占比 ≥ 8/20 的日子够多时够用 | **若长期 < 8/20（当前常态是 6/20 左右），说明以条为单位已无信息量 → 走 B** |

建议：**P0 先做 A（成本极低），同时用一周数据验证占比；若持续低于 8/20，P2 直接切 B。** 不要一上来就重写，先让指标说话。

---

## 5. 明确不做（以及为什么）

| 不做 | 理由 |
|---|---|
| 增加新闻源 / 增加板块 | 供给不是瓶颈。120 条里只有 6 条命中主题，瓶颈在主题关键词覆盖（该走 `theme_miss_scan.py` 补词），不在条数 |
| 让 LLM 现场"解释为什么涨" | 同一份数据每天会给出不同说法，榜单不可复现，成绩单失去意义。解释必须来自已结构化的字段 |
| 把三榜合成一个"综合分" | 三者时间尺度不同（事件 / 结构 / 标的），合分会**掩盖矛盾**——而矛盾恰恰是最有价值的信号（新闻利好但资金流出，正是"伪共振"） |
| 给量化库既有表加列 | 没有 Alembic 链路，`create_all` 只建新表。本方案唯一新增的是 `quant_pool_snapshot`，新表，重启即生效 |
| 榜单直接控制下单 | 项目已经在"硬门槛掐死全天交易"上吃过亏。榜单只影响**展示与排序**，最多通过既有的 `dyn_confidence` / `driver_weight_coef` 软性传导 |

---

## 6. 指标

**主指标**：榜单点击穿透率（榜 → 诊股 / 详情 / 证据链）。
**次要指标**：可交易条目占比、历史回看次数、成绩单覆盖条数占比。

**反向指标（更重要）**：**榜首 5 条被买入的比例**。
如果长期接近 0，说明榜单和交易是两套系统——那所有优化都只是把橱窗擦得更亮。

---

## 7. 改动清单（文件级，供排期估算）

| 层 | 文件 | 量级 |
|---|---|---|
| P0 | `backend/app/services/pipeline_service.py`、`frontend/src/views/ImpactRanking.vue`、`IndustryRanking.vue`、`Home.vue`、`ai_stock/quant/agents/selection.py` | ~150 行（改） |
| P1 | `ai_stock/quant/db_models.py`（+1 表）、`ai_stock/quant/db_ops.py`（快照写读）、3 个 API、3 个页面 | ~500 行 |
| P1.5 | `backend/app/api/board.py`（新）、`frontend/src/components/EvidenceChain.vue`（新）、`ai_stock/pipeline/db_ops.py`（归因统一，**删代码**） | ~350 行 |
| P2 | `scripts/news_impact_review.py`（抽函数复用）、`/api/board/scorecard`、各页面入口 | ~300 行 |

---

## 8. 实现记录（已落地）

四层全部实现，实际改动与上述清单的偏差见「偏离说明」。

### 8.1 新增文件

| 文件 | 职责 |
|---|---|
| `backend/app/services/board_view.py` | 展示口径 + 通用跨期 diff。**纯函数、不 import DB**：`annotate_pool`（rank / dyn_state / is_tail / near_kick / expire_in_days）、`diff_ranked`、`_diff_summary`、`consecutive_days` |
| `backend/app/services/board_chain.py` | 证据链聚合。`build_chain(theme, date)` → `{theme, news[], industry, leaders[], pool[], notes[]}`；主题命中只看结构化 `industry_impacts` / `industries`，**不做标题子串匹配** |
| `backend/app/services/board_scorecard.py` | 三榜成绩单。`MIN_SAMPLES=30` 的样本闸门写在每个 block 上（`sample_ok`），未达标一律 `rate=None` |
| `backend/app/api/board.py` | 只读路由 `/api/board/*`：`industry/diff`、`impact/diff`、`recommendation/diff`、`chain`、`cluster`、`scorecard` |
| `frontend/src/api/board.js` | 上述接口的前端封装 |
| `frontend/src/components/BoardDiffBar.vue` | 新进 / 最大升 / 最大降 / 掉榜 / 无变化 chips，`has_previous=false` 时降级为虚线提示 |
| `frontend/src/components/BoardScorecard.vue` | 三榜成绩单弹窗；`rateText()` 在样本不足时显示「样本不足」而不是 0% |
| `frontend/src/components/EvidenceChain.vue` | 新闻 → 行业 → 龙头 → 池内 四段式证据链，复用 `ImpactDetail` |

### 8.2 新增表（量化链路无 Alembic，只能新建表）

- `quant_pool_snapshot`（`ai_stock/quant/db_models.py`）：`trade_date + symbol` 唯一，存 `confidence / dyn_confidence / stage_batch / status / radar_theme`。由 `ConfidenceMaintainAgent` 在维护结束后落盘 —— 这是本轮**唯一的写路径**，目的是让「昨天的池子长什么样」可复现。
- `news_cluster_member`（`ai_stock/pipeline/db_models.py`）：同源事件的多篇报道落明细，支撑榜单上的 `×N` 展开。`news_items` 加不了列，所以单开表。

### 8.3 关键口径修正

1. **热股榜排序按 `dyn_confidence`**，与 `buy.py:212` 的下单排序一致；`annotate_pool` 输出的 `rank` 才是界面名次。原先按静态 `confidence` 排，榜单一二名可能根本不是最先被买的。
2. **`dyn_state` / `is_tail` 与雷达同源**：`CONF_KICK_THRESHOLD=0.25` 以下显式标「鱼尾」，`near_kick`（×1.5 以内）提前预警。常量在 `board_view` 里复刻并注明出处，避免服务层反向依赖 quant 包。
3. **龙头「领涨」不再是 index-0**。`selection.leader_board_labels()` 要求个股涨幅**严格超过板块涨幅**才给「领涨」；`board_pct is None` 时一律不给。同时带「精选」标签，两个标签共存时「领涨」优先。
4. **新闻来源徽章**：`assess_source`（rule / llm / recheck）直接显示在榜单行，替代原来靠 `confidence<=0.31` 猜「是否回填」的做法 —— 猜阈值迟早错。
5. **行业→新闻归因统一**：`db_ops.get_news_by_industry_ranking` 的匹配优先级固化为 `industry_impacts → 存量字段 → 知识库别名 → 正文匹配`，并把 `matched_by` 写回条目，出现误配可追溯。

### 8.4 偏离说明

- 设计稿提到的「新闻榜单元 = 主题」激进方案**未做**，理由不变：需要先积累一周的 `可交易信号 N/20` 真实数据，否则是拍脑袋改排序单位。
- `get_hot_stocks_latest` 仍读**实时自选池**（`get_optional_pool("active")`），只有 `by_date` 才读快照。这是有意为之：当日榜单要反映最新维护结果，历史回看才需要可复现。
- 成绩单是**只读**的：不写回任何阈值，`sample_ok=False` 时前端也不显示 rate。这条纪律写在 `board_scorecard.py` 的模块 docstring 里，防止后人「顺手」加自动调参。

### 8.5 测试

- `backend/tests/test_board_views.py`（12 例）：dyn 排序、鱼尾/临界、四种变化态、`no_previous_diff` 的诚实降级、`consecutive_days` 断档即停、成绩单时间方向（超额 +7.5 / -7.5）、池存活率取样方向。
- `tests/test_board_leader_labels.py`（6 例）：「领涨」必须跑赢板块、无板块涨幅时不打标、双标签共存、不修改入参。
