# 量化链路缺陷修复与改进需求规格书

> 版本：v1.0 ｜ 编制日期：2026-10-21
> 范围：`ai_stock/quant/` 买入—持仓—卖出链路 + 新闻主题链路
> 依据：代码走查（行号对应 2026-10-21 工作区 HEAD），未做运行时验证的条目已单独标注
> 关联文档：`docs/QUANT_FIX_PLAN.md`（2026-10-17 故障复盘）、`docs/THEME_TAXONOMY_DESIGN.md`

## 0. 阅读约定

- 每条需求含：**类型**（Bug / 改进 / 决策）、**优先级**（P0 影响资金安全 / P1 结构性 / P2 打磨）、
  **现状与证据**、**需求描述**、**验收标准**、**实现提示**、**风险与回滚**。
- “证据”一律给 `文件:行号`，工程同学可直接跳转核对。凡标注「**待确认**」的，表示代码行为与
  业务意图是否一致需要产品/投研拍板，不得由开发单方面按“看着像 bug”修改。
- 本规格书**不包含**下述已在 `QUANT_FIX_PLAN.md` 中闭环的事项：买入扫描池截断、配置种子不覆盖、
  预筛与步骤判据不同源、二次验证“利空消除”死锁。
- 生产口径沿用既有约定：`max_drawdown_pct = 0.10`（人工设定的更严口径，**不要**对齐回代码种子 0.20）；
  `market_timing_enable` 当前为 `0`（临时关闭，见 FR-10 的验收前提）。

---

## 1. 需求总览

| 编号 | 类型 | 优先级 | 模块 | 一句话 | 影响 |
|---|---|---|---|---|---|
| FR-01 | Bug | **P0** | risk_control | 账户回撤峰值口径错误，盈利状态下回撤恒为 0，冻结与减仓闸门失效 | 资金安全 |
| FR-02 | Bug | **P0** | hold/risk | 卖出被风控/跌停/行情故障拒绝后无重试无告警，止损可能完全失效 | 资金安全 |
| FR-03 | Bug | **P0** | risk_control | 单日亏损只统计已实现盈亏，浮亏不计入，日内风控形同虚设 | 资金安全 |
| FR-04 | 决策 | **P0** | buy | 二次验证 `risk_reward` 缺失时 fail-open 放行，与买入链路整体 fail-closed 不一致 | 资金安全 |
| FR-05 | 改进 | P1 | hold | 止盈价只写不读，无主动止盈；移动止损仅一档 | 收益回吐 |
| FR-06 | 决策 | P1 | hold | “收盘确认”实际用 provisional bar，盘中即触发，与注释声明的“盘中只预警”矛盾 | 止损时机 |
| FR-07 | 改进 | P1 | hold | 卖出侧无对称确认：单次 LLM 判定重大利空即清仓 | 误杀 |
| FR-08 | 改进 | P1 | market_regime | 择时门单指标 whipsaw；`unknown` 无持续失效补偿 | 交易节奏 |
| FR-09 | 改进 | P1 | broker | 实盘适配器层只有 4 项权限检查，完整风控依赖调用方自觉 | 实盘安全 |
| FR-10 | 改进 | P2 | buy/news | 冷却窗口按“最近 300 条决策”扫描，高吞吐下被条数截断 | 成本/噪音 |
| FR-11 | Bug | P2 | news_impact | 新闻新鲜度用主机本地时间对比北京时间，服务器时区非东八区时系统性偏差 | 榜单排序 |
| FR-12 | 改进 | P2 | theme_registry | 主题闸门的“可用日线”门槛允许 8 只成员只有 5 只有历史 | 统计可信度 |
| FR-13 | 改进 | P2 | broker | 模拟盘在跌停价直接拒卖，与真实市场（可挂单排队）不符 | 模拟净值可信度 |
| FR-14 | 改进 | P2 | 全局 | 交易链路缺归因出口；已有回测组件未接入调度 | 可优化性 |

---

## 2. P0 需求

### FR-01 账户回撤改用历史净值高点

**类型**：Bug ｜ **优先级**：P0 ｜ **影响**：回撤冻结、仓位系数减半、异步强减三处闸门

**现状与证据**

`ai_stock/quant/risk_control.py:61-65`：

```python
initial = float(db_ops.get_config_value("initial_cash", "1000000"))
drawdown = 0.0
peak = max(initial, info.total_assets)  # 简化峰值: 初始资金 vs 当前
if peak > 0:
    drawdown = (peak - info.total_assets) / peak
```

`peak` 取「初始资金」与「当前总资产」的较大值，等价于：**只要账户处于盈利状态，回撤恒为 0**。
账户从 100 万涨到 150 万再跌到 120 万，正确回撤是 20%，当前实现算出来是 0。

三处下游全部失效：

1. `risk_control.py:117-119`：`drawdown_pct >= max_drawdown_pct(0.10)` 触发 `trade_frozen=1` —— 盈利后永不可能触发；
2. `buy.py:1195-1203`：回撤 ≥ 6% 时仓位系数 × 0.5 —— 同上；
3. `risk_control.py:377-379`：异步风控的同类冻结 —— 同上。

**需求描述**

1. 引入账户历史净值高点（HWM），持久化到 `quant_system_config`（键名建议 `account_hwm`）。
2. `account_snapshot()` 每次调用时：`hwm = max(已存 hwm, 当前 total_assets)`，并写回；回撤 = `(hwm - total_assets) / hwm`。
3. HWM 只在账户**重置/注资/出金**时允许人工调整，且调整动作必须写 `quant_agent_decision_log`。
4. 保留一个只读观测字段 `drawdown_basis`，值为 `hwm` 或 `initial`，便于事后判断口径。

**验收标准**

| 用例 | 输入 | 期望 |
|---|---|---|
| 涨后回撤 | HWM=150 万，当前 120 万 | `drawdown_pct = 0.20`，买入被拒且 `trade_frozen=1` |
| 涨后浅回撤 | HWM=150 万，当前 146 万 | `drawdown_pct ≈ 0.0267`，买入通过，仓位系数不折半（阈值 6%） |
| 纯下跌 | HWM=100 万，当前 92 万 | `drawdown_pct = 0.08`，未触发 10% 冻结 |
| 首次上线 | 库中无 `account_hwm` | 以 `max(initial_cash, 当前总资产)` 初始化，**不得**首日误冻结 |
| 账户重置 | 人工重置模拟盘 | HWM 同步重置，不残留旧高点导致永久冻结 |

**实现提示**

- 迁移首日必须先用 `max(initial_cash, 当前总资产)` 初始化，否则历史盈利账户会立即触发 10% 冻结。
- `account_snapshot()` 被 `pre_trade_check` 高频调用（每笔下单一次），写配置需注意并发：建议只在 `hwm` 变化时写库，且走已有的 `set_config_value`（已带事务）。
- 生产 `max_drawdown_pct=0.10` 不得修改。

**风险与回滚**

修复后回撤数值会**变大**，可能触发历史上从未触发过的冻结。灰度方式：先把 `max_drawdown_pct` 临时维持 0.10、
在模拟盘观察 5 个交易日冻结次数；若单日多次触发，先查是真实回撤还是 HWM 初始化错误，**不要**直接放宽阈值。
回滚开关：新增 `drawdown_basis` 配置项，设为 `initial` 可退回旧口径。

---

### FR-02 卖出失败的重试、降级与告警

**类型**：Bug ｜ **优先级**：P0 ｜ **影响**：止损可靠性

**现状与证据**

三条卖出路径在被拒后都只写日志、不做任何重试：

- `ai_stock/quant/agents/hold.py:500-506`：`pre_trade_check` 拒绝 → `self.log("sell_risk_rejected")` → 返回；
- `ai_stock/quant/agents/risk.py:80-86`：强制减仓同理；
- `ai_stock/quant/broker.py:344-346`：模拟盘在跌停价直接 `return self._reject(... "跌停价 ... 不可卖出")`。

被拒的两个高频原因：

1. `risk_control.py:165-167`：`quote["price"] <= limit_down` → 拒卖；
2. `risk_control.py:130-136`：行情取不到 → `quote=None` → **买卖一律拒单**。

急跌行情下，持仓破位 → 卖单被拒 → 下一轮 `hold_scan` 在 10 分钟后重新决策 → 再被拒，
循环期间价格持续下跌。极端情况下止损**完全没有成交机会**，且没有任何 ERROR 级告警，
事后只能靠翻决策日志发现。

**需求描述**

1. **状态化退出意图**：卖出被拒时，在持仓记录写入 `pending_exit`（含首次触发时间、累计失败次数、触发原因），
   后续每轮 `hold_scan` 优先处理 `pending_exit`，而不是重新走一遍完整决策链。
2. **分级重试**：
   - 跌停导致拒卖：保留意图，每轮重试；累计失败 ≥ 3 轮升级为 ERROR 告警（含标的、跌停价、累计时长）；
   - 行情不可得：卖单允许降级——使用 `data_service` 已有的降级行情（`_degraded_quote_from_ohlcv`，
     `data_service.py:595`）估值后放行，或直接标记 `pending_exit` 等待恢复；**不得**静默丢弃；
   - 其他（T+1、笔数上限）：保留意图即可，不告警。
3. **告警出口**：复用 `ops_monitor.get_alerts()`，新增事件类型 `sell_blocked`（ERROR）与 `sell_blocked_persistent`（CRITICAL）。
4. **不得**通过放宽 `trading_session_guard_enable` 来实现盘后补单（该闸门为买卖同闸，属既有正确设计）。

**验收标准**

1. 构造持仓 + 跌停行情，连续 3 轮 `hold_scan`：每轮均产生卖出尝试，第 3 轮出现 `sell_blocked` 告警，
   持仓记录中 `pending_exit.fail_count` 递增。
2. 构造行情接口异常（`get_realtime_quote` 抛异常）：卖单不再返回“无法获取行情, 拒绝下单”，
   而是走降级行情或进入 `pending_exit`，并产生一条 WARNING。
3. 跌停打开后的第一轮扫描即成交，`pending_exit` 被清除。
4. 单测覆盖：跌停拒卖 → 重试 → 成交的完整状态迁移。

**实现提示**

- `hold.py:68` 的 `handle_hold_scan` 当前对每只持仓无条件启动 flow。改为：先查 `pending_exit`，
  存在则直接进入 `ExecutionUpdateAgent` 的卖出分支（跳过 LLM 决策），避免重复烧 LLM 且保证意图不丢失。
- 有 `pending_exit` 时，`reduce_position` 的决策结果应被忽略（价格已破位，不应因为当日反弹就撤销退出意图）。
- 清理时机：成交、人工撤单、或持仓消失时清除 `pending_exit`。

**风险与回滚**

- 重试会在跌停日产生大量拒单日志，需限频（每标的每轮最多 1 次尝试）。
- 若担心“跌停打开瞬间被连续砸单”，可加 `max_retry_per_day`，默认 20。
- 回滚：`pending_exit` 为附加字段，删除处理分支即回到现状。

---

### FR-03 单日亏损计入浮亏

**类型**：Bug ｜ **优先级**：P0 ｜ **影响**：单日亏损冻结

**现状与证据**

- `risk_control.py:71`：`"daily_pnl": round(db_ops.get_realized_pnl_today(), 2)`；
- `db_ops.py:657-679`：`get_realized_pnl_today` 只汇总 `side == "sell" and status == "filled"` 的
  `realized_pnl`（及其兜底近似），**不含任何持仓浮亏**；
- `risk_control.py:120-124`：`daily_pnl < -initial * max_daily_loss_pct(0.03)` 才冻结。

结论：持仓当日跌 8% 但不卖出，`daily_pnl = 0`，单日亏损闸门不触发。
该闸门的实际语义是“当日已实现亏损超 3% 才冻结”，与配置名 `max_daily_loss_pct` 的直觉不符。

**需求描述**

1. 新增 `daily_pnl_total = 当日已实现盈亏 + Σ(持仓数量 × (现价 − 昨收))`。
2. 昨收取 `get_realtime_quote` 的 `prev_close`；缺失时用日 K 倒数第二根收盘价；仍缺失则该标的浮亏按 0 计，
   并在 `details` 中标注 `mtm_incomplete=[symbols]`。
3. 冻结判据改用 `daily_pnl_total`；`daily_pnl`（已实现）保留为观测字段，不要删除（历史看板在用）。
4. 冻结触发后仍需人工解除（既有语义不变）。

**验收标准**

| 用例 | 场景 | 期望 |
|---|---|---|
| 浮亏触发 | 持仓市值 60 万，当日跌 6%，无卖出 | `daily_pnl_total ≈ -3.6 万`，超 3 万阈值 → 冻结 |
| 已实现触发 | 卖出亏损 4 万，持仓无变化 | 冻结（回归原行为） |
| 数据缺失 | 一只持仓昨收缺失 | 不因缺失误冻结，`details.mtm_incomplete` 含该标的 |

**实现提示**

- 计算位置应放在 `account_snapshot()` 内（已持有 holdings 与 broker 数据的调用上下文），
  避免每个调用点各算一遍。
- 注意性能：`account_snapshot` 每笔下单调用一次，浮亏计算应复用 `broker.get_positions()` 的
  `last_price`，不要逐票再打行情接口。

**风险与回滚**

浮亏计入后，大盘单日大跌会让冻结更频繁触发。若观察到“上午即冻结、下午反弹”的频繁误伤，
应调整的是 `max_daily_loss_pct`（需投研决策），而不是退回只算已实现、口径更宽松的实现。

---

### FR-04 二次验证 `risk_reward` 缺失时的处理口径

**类型**：决策（当前为有意的 fail-open）｜ **优先级**：P0 ｜ **影响**：买入质量

**现状与证据**

- `buy.py:1100-1104`：`weak_rr = MIN_RISK_REWARD_RATIO > 0 and 0 < rr < MIN_RISK_REWARD_RATIO`
  —— `rr == 0` 时**不**判为不达标；
- `buy.py:129-135` 的文档字符串明确写道：“解析不出来返回 0.0 —— 调用方据此跳过校验，不用猜测值否决交易”；
- `buy.py:119` 的 `risk_reward` 字段默认值 `0.0`，LLM 不填即为 0。

因此存在一条静默路径：LLM 未输出数值型 `risk_reward`（或输出无法解析的文本）→ `rr=0` → 风控收益比校验**跳过** →
只要 LLM 自己说 `passed=true` 就放行。实测频率未知（需统计），但这条路径与其他环节的 fail-closed 取向不一致：
`SecondVerificationAgent` 在 LLM 不可用时是直接拒绝建仓的（`buy.py:1071-1078`）。

**需求描述（需投研决策，二选一）**

- **方案 A（建议）**：`rr` 缺失视为不达标，但先重试一次（提示词强调“必须输出纯数字 risk_reward”）；
  两次都缺失则拒绝，`reject_stage = "rr_missing"`。
- **方案 B**：保持 fail-open，但必须留痕——`reject_stage` 体系增加 `rr_missing` 计数，
  并在周报中披露“因收益比缺失而放行的笔数”。

无论选哪个方案，都需要在 `quant_agent_decision_log` 的 `detail` 中记录原始 `risk_reward_ratio` 文本，
以便事后核对解析是否正确（特别是 `1:1.4` 与 `2.6:1` 两种记法的方向）。

**验收标准**

1. 构造 LLM 返回 `risk_reward=0, passed=true`：方案 A 下必须被拒且 `reject_stage="rr_missing"`；方案 B 下必须有一条计数记录。
2. 构造 `risk_reward_ratio="1:1.2"`：解析为 1.2（收益/风险），判为不达标被拒。
3. 构造 `risk_reward_ratio="3:1"`：解析为 3.0，通过。
4. 统计脚本能输出近 30 天 `rr_missing` 占比。

**实现提示**

`_parse_risk_reward`（`buy.py:129`）现有冒号逻辑取“大数/小数”，`1:1.2` 会得到 1.2、`1:0.8` 会得到 1.25，
在记法反了的情况下会**系统性高估**。建议：若文本含冒号且左侧为 1，按“风险:收益”解释；否则按“收益:风险”解释，
并在 detail 中记录采用的解释方式。

**风险与回滚**

方案 A 会降低开仓数量。建议先行统计 30 天内 `rr` 缺失占比，若 <5% 直接切方案 A；若占比较高，
说明提示词或结构化输出有问题，应先修提示词（`PROMPT` 在 `buy.py:1049` 起，注意升版本号）。

---

## 3. P1 需求

### FR-05 止盈与多档移动止损

**类型**：改进 ｜ **优先级**：P1

**现状与证据**

- `buy.py:1340`/`1347`：建仓时计算 `take_profit = fill_price × (1 + take_profit_pct/100)` 并落库；
- 全仓检索 `take_profit` 的读取点：仅 `hold.py:91` 将其塞进 flow context，
  **`ReducePositionAgent` 从未与 `take_profit` 比较**（`hold.py:341-387` 全文无引用）；
- 移动止损只有一档：`hold.py:268-277`，`gain_ratio >= TRAILING_STOP_TRIGGER(0.15)` 时止损上移至成本 +10%。

后果：盈利 3%~15% 区间没有任何保护；达到 15% 后止损一次性抬到 +10%，中间档位缺失；
`take_profit` 字段是纯展示数据，容易让使用者误以为存在止盈逻辑。

**需求描述**

1. **分档移动止损**（参数进 `config.py`，数值待投研确认，下表为建议初值）：

| 浮盈区间 | 止损位上移至 |
|---|---|
| ≥ 6% | 成本价（保本） |
| ≥ 12% | 成本 × 1.06 |
| ≥ 20% | 成本 × 1.12，或改为跟踪最高价回撤 8%（二选一） |

2. **止盈语义二选一（需决策）**：
   - 方案 A：`take_profit` 仅作参考，不做主动卖出（当前行为），但需在前端/文档明确标注“非交易指令”；
   - 方案 B：触及 `take_profit` 时减仓 1/2（不清仓），余仓交给移动止损。
   建议 B：与“让利润奔跑、但不裸奔”的思路一致，且不清仓避免踏空。
3. 止损位只允许上移（已由 `hold.py:624` 的 `new_stop > holding.stop_loss` 保证，保持）。
4. 每次上移写 `plan.history` 与决策日志（现有 `_update_plan` 已具备，扩展档位字段即可）。

**验收标准**

1. 浮盈 7% → 止损位 = 成本（原为建仓时的 ATR/5% 止损位，需确保上移生效）。
2. 浮盈 13% → 止损位 = 成本 × 1.06。
3. 浮盈 6% 后回落到 4%，止损位**不**下移。
4. 方案 B 下，触及止盈价当日触发 `sell_partial` 0.5，持仓数量减半，止损位保持不变。

**实现提示**

- 档位判断应放在 `AddPositionAgent`（已有 `gain_ratio` 与 `current_close`），不要在
  `ReducePositionAgent` 里重算，避免两处口径漂移（项目已多次因判据不同源踩坑）。
- 注意 `gain_ratio` 的取价问题见 FR-06。

**风险与回滚**

保本止损会在震荡市提高被扫出的概率。上线后需对比“移动止损触发后 5 日的标的表现”，
若普遍继续上涨，说明档位过紧。回滚：档位表置空即回到单档逻辑。

---

### FR-06 “收盘确认”与 provisional bar 的语义对齐

**类型**：决策（当前实现与注释矛盾）｜ **优先级**：P1

**现状与证据**

- `data_service.py:536-559`：`get_stock_indicators(live=True)` 为默认，盘中会把实时价拼成当天
  provisional bar（`bar_date == 今天`、`provisional=True`）；
- `hold.py:113`：`TrendTrackingAgent` 调 `get_stock_indicators(symbol)`，未传 `live`，即**盘中含 provisional bar**；
- `hold.py:125`：`current_close = close_series[-1]` —— 盘中该值是**实时价**；
- `hold.py:168-174`：`confirm_bars = STOP_LOSS_CONFIRM_BARS(1)`，`stop_confirmed` 取
  `close_series[-1:]`，即用实时价与止损位比较；
- `hold.py:176-181`：注释写“盘中破位预警（未收盘确认）：只留痕，不触发卖出”，
  但同一函数内 `stop_loss_signal` 已在盘中成立，`intraday_breach` 实际上几乎不会被单独观察到。

结论：所谓“收盘确认”在盘中退化为“实时价确认”。这不是崩溃，但会导致：
① 盘中假破位被卖出；② 回测（`evolution.backtest_stop_policy` 明确用收盘价，见其 docstring）
与实盘行为不一致，参数回测结论无法直接迁移。

**需求描述（需决策，二选一）**

- **方案 A（严格收盘确认）**：止损/移动止损判定改用 `live=False` 的已完成 bar；
  盘中破位只写 `intraday_breach` 并触发预警通知，卖出在收盘后或次日开盘执行。
  代价：急跌日多承担一天的跌幅（这正是 `hold.py:159-161` 注释中“原实现要求连续 2 根收盘破位，
  急跌中实际亏损可达 8~12%”想避免的）。
- **方案 B（明确盘中确认为准）**：承认盘中即卖出，删除“只留痕”注释，改为在文档与前端明示
  “止损为盘中实时触发”，并把 `evolution.backtest_stop_policy` 的口径同步为“盘中触价”或在回测中使用
  `Low` 列判断（`get_ohlcv` 已有 `Low`）。

**建议**：先做 B（改动小、与现有注释中“急跌止损优先”的取向一致），把回测口径修正为用 `Low` 判破位；
若后续投研希望严格收盘确认，再切 A。

**验收标准**

1. 选定方案后，文档（`docs/TECHNICAL.md` 或本文件追加章节）明确写出“止损判定使用哪个价格”，
   代码注释与实现一致，不得再有“盘中只留痕”与“盘中即卖出”并存的表述。
2. 若选 B：`backtest_stop_policy` 的破位判断改用 `Low`，并补一条单测证明回测与实盘触发条件一致。
3. 若选 A：盘中破位产生一条 WARNING/通知，且当日不产生卖单。

**风险与回滚**

方案 B 维持现状，无风险。方案 A 会显著改变止损时点，必须先在模拟盘对比 10 个交易日的差异再上线。

---

### FR-07 卖出侧的确认机制

**类型**：改进 ｜ **优先级**：P1

**现状与证据**

- `hold.py:364-372`：无硬止损时，`_check_major_bear` 返回 `has_major_bear=True` → 直接 `sell_all`，
  **跳过全部技术指标**；
- `hold.py:389-421`：LLM 单次结构化调用即决定，无二次确认、无连续轮次要求；
- 对比买入侧：`KICK_CONFIRM_ROUNDS = 2`（`config.py:172`）要求连续两次命中才移除标的，
  注释明确写“LLM 单次采样波动不得直接移除标的”。

即：**卖出比买入更激进**。LLM 一次误判（把旧闻识别为新增重大利空）就会清掉一个正在盈利的仓位。

另需明确的现状（**不是缺陷**）：LLM 不可用时 `_check_major_bear` 返回
`has_major_bear=False`（`hold.py:401-405`），即不卖——这个 fail-safe 方向是对的，无需改动。

**需求描述**

1. 重大利空清仓改为**两段式**：
   - 第一段：LLM 判 `has_major_bear=True` → 当日不立即清仓，而是写入 `pending_exit(reason="major_bear", confirm_needed=1)`，
     按 FR-02 的机制在下一轮扫描复核；
   - 第二段：下一轮再次判定为真（或当日跌幅 > 3%）→ 执行清仓。
2. 若该标的已处于硬止损状态（`stop_loss_signal`），第一步即清仓，不受本机制约束（避免延迟止损）。
3. `ReducePositionAgent` 与买入侧对称地记录 `reject_stage`/`hold_stage`，便于统计“因利空被拦下但随后上涨”的比例。

**验收标准**

1. 单次 LLM 判利空：当日不减仓，持仓记录出现 `pending_exit` 且 `reason` 含 `major_bear`。
2. 连续两次判利空：清仓，日志中出现两次 `major_bear` 判定记录。
3. LLM 不可用：不产生卖出（回归现有行为）。
4. 统计脚本可输出“major_bear 首次判定 → 最终是否清仓”的转化率（用于评估误报率）。

**风险与回滚**

延迟一天清仓意味着真利空时多亏一天。可用“当日跌幅 > 3% 直接清仓”的旁路兜底（已写入需求 1）。
回滚：`confirm_needed=0` 即恢复单次判定。

---

### FR-08 择时门的稳定性与失效补偿

**类型**：改进 ｜ **优先级**：P1

**现状与证据**

- `market_regime.py:86-98`：`close > MA20 且 MA20 三日内上翘` 才 `risk_on`，其余全为 `risk_off`；
- `risk_control.py:299-300`：`not regime.get("ok")`（数据不可用）时**不拦截**；
- `market_regime.py:76-84`：日线不足 `window+3` 即 `unknown`。

两个已知短板：
1. **whipsaw**：指数在 MA20 附近震荡时，闸门会逐日开关，导致买入流程时有时无（不是错误，但节奏不可控）；
2. **失效无补偿**：数据源持续故障时，择时门静默失效，系统在熊市中照常开仓，且无人知晓。

**需求描述**

1. **加确认与缓冲**：`risk_on` 需连续 2 个交易日满足条件（或收盘高于 MA20 达 1% 以上）才放行；
   `risk_off` 立即生效（风控方向不对称是合理的：收紧要快，放松要慢）。
2. **unknown 补偿**：`unknown` 连续超过 N 小时（建议 24h）→ 发 WARNING 告警；
   连续超过 3 个交易日 → 仓位系数 × 0.5（只收紧，不阻断）。状态需持久化，不能只靠内存缓存。
3. `regime` 计算结果与 `unknown` 时长写入决策日志，便于事后复盘“择时门当时到底是开是关”。

**验收标准**

1. 构造指数在 MA20 上下 0.5% 震荡的 5 日序列：`risk_on` 只出现一次（第 2 日），不出现逐日翻转。
2. 构造数据源返回空：第 1 日不阻断；24h 后产生告警；第 3 个交易日仓位系数变为 0.5。
3. 生产开关 `market_timing_enable=0` 时，上述逻辑全部不生效（保持运行时开关语义）。

**风险与回滚**

“连续 2 日确认”会让底部反转时的首次建仓延后 1 天，需投研接受。
`unknown` 收紧仓位在数据源长期故障时会压低交易频率，属预期行为，告警需可关闭。

---

### FR-09 实盘适配器层的风控收口

**类型**：改进 ｜ **优先级**：P1（上实盘前必须完成）

**现状与证据**

- `broker.py:508-525` `_ensure_live_permission`：只检查 全局开关 / 单日笔数 / 单笔金额 / 白名单 四项；
- 完整风控 `pre_trade_check` 由**调用方**负责：`buy.py:1262`、`hold.py:500`、`hold.py:567`、`risk.py:80`
  四处都调用了，因此当前主链路是安全的；
- 但 `HuaxingBrokerAdapter.place_order`（`broker.py:564-584`）本身不校验回撤冻结、行业集中度、择时门、T+1。

**需求描述**

1. 在 `place_order` 内对 `OrderSide.BUY` 增加“兜底调用 `pre_trade_check`”的第二次检查（幂等，重复调用无副作用），
   或至少在 `_ensure_live_permission` 中补齐 `trade_frozen`、`max_drawdown_pct`、`INDUSTRY_CONCENTRATION_MAX` 三项。
2. 增加显式的“实盘装配自检”：`set_broker(HuaxingBrokerAdapter(...))` 时校验风控版本号/开关，
   未通过则拒绝装配并报错，避免误装配。
3. 文档（`docs/DEPLOYMENT.md`）补充实盘上线检查清单：开关、白名单、单笔上限、回撤阈值逐项确认。

**验收标准**

1. 直接构造一笔超额买入订单调用 `broker.place_order`（绕过业务层）：被拒。
2. `trade_frozen=1` 时直接调用 `place_order(BUY)`：被拒。
3. 装配自检失败时进程启动即报错，不会静默降级到模拟盘继续跑（当前若 `set_broker` 未调用则一直是模拟盘，
   这一点需在启动日志中明确打印当前 broker 类型）。

**风险与回滚**

`pre_trade_check` 在 broker 层二次调用会增加一次账户快照开销（可传入已取快照避免）。
注意：`pre_trade_check` 内含时段闸门，盘后实盘下单会被拒——这正是期望行为。

---

## 4. P2 需求

### FR-10 冷却窗口改为按时间查询

**现状与证据**：`buy.py:294-320`，`db_ops.get_decisions(limit=300, agent="second_verification",
decision="verification_rejected")`，然后按 240 分钟过滤。高吞吐时最近 300 条可能只覆盖几分钟，
冷却被人为缩短；低吞吐时又拉取过多。

**需求**：`get_decisions` 增加 `since` 参数（按时间过滤），冷却查询改为
`since = now - BUY_REJECT_COOLDOWN_MINUTES`，`limit` 提到 2000 作为上限保护。

**验收**：构造 1000 条更早的拒绝记录 + 1 条 200 分钟前的记录，冷却集合必须包含该标的；
构造 1 条 300 分钟前的记录，必须不在集合内。

---

### FR-11 新闻新鲜度的时区口径

**现状与证据**：`news_impact.py:842-853`

```python
dt = datetime.strptime(str(pub_time)[:16], "%Y-%m-%d %H:%M")   # 北京时间 naive
hours = (datetime.now() - dt).total_seconds() / 3600.0          # 主机本地时间
```

`pub_time` 来自东财/同花顺/新浪，均为北京时间 naive；`datetime.now()` 取主机本地时间。
服务器若为 UTC，`hours` 恒少 8 小时，`hours < 0` 时直接 `return 1.0`（`news_impact.py:851-852`），
即**下午发布的新闻新鲜度恒为满分**，48 小时衰减对当天新闻完全失效。

**需求**：统一为东八区计算，与 `risk_control._now_market()` 同一口径
（`risk_control.py:34-39` 已有实现，建议抽到公共模块复用，避免第三份时区代码）。

**验收**：
1. 主机时区设为 UTC 时，北京时间 1 小时前的新闻 `freshness ≈ 0.979`；49 小时前 `≈ 0.1`（下限）。
2. 主机时区设为东八区时结果一致。
3. 单测使用固定时间戳 + 冻结时钟，不依赖运行时区。

---

### FR-12 主题闸门的样本量下限

**现状与证据**：`theme_registry.py:321`

```python
if not hist or len(hist) < min(5, len(symbols)):
```

`symbols` 至少 8 只（`GATE_MIN_MEMBERS=8`），因此该门槛实际恒为 5：
8 只成员只要 5 只有 ≥25 根日线就通过“可用日线”关，后续相关性（`GATE_MIN_INTERNAL_CORR=0.35`）与
离散度（`GATE_MAX_DISPERSION=1.6`）都建立在这 5 只上。小样本下相关性方差极大，
一个 5 只样本的均值相关 0.35 可能完全由 1~2 对股票决定。

**需求**：把门槛改为“覆盖比例 + 绝对下限”双条件，建议 `len(hist) >= max(6, ceil(0.7 * len(symbols)))`；
不满足时 `status = shadow`（而非 `rejected`），并在 `metrics` 中记录 `coverage`。

**验收**：8 只成员仅 5 只有历史 → 不再通过（进入 shadow 或 rejected，取决于实现选择）；
8 只中 7 只有历史 → 通过，`metrics.coverage = 0.875`。

**注意**：本项目历史上有“家电主题用换成分股凑闸门”的教训，本次修改方向相反（收紧），
不会重蹈覆辙；但需回归 `theme_audit.py` 确认现有 24 个主题是否会被打回，打回的必须先看覆盖率数据再决定是否接受。

---

### FR-13 模拟盘跌停卖出的建模

**现状与证据**：`broker.py:344-346`，`price <= limit_down` 时直接拒单。真实市场允许在跌停价挂卖单排队，
只是大概率不成交；模拟盘的“直接拒单”会让回测/模拟净值高估平仓能力（极端行情下真实账户会被套住，模拟盘则表现为“卖不出”）。

**需求**：跌停卖单改为 `OrderStatus.PENDING`（挂单），并记录 `pending_reason="limit_down_queue"`；
后续扫描时，若现价高于跌停价则按现价成交，否则保持挂单。若希望更保守，可用“当日跌停则按次日开盘价成交”
的简化模型，但必须在文档中写明模型假设。

**验收**：构造跌停持仓，卖单返回 `pending` 而非 `rejected`；次日开盘价成交，成交价 = 开盘价（含滑点）。
模拟账户净值曲线中不得再出现“跌停日成功按跌停价卖出”的记录。

---

### FR-14 交易链路的归因出口（对上一版结论的更正）

**先更正事实**：项目**已有**部分回测与归因能力，不是空白——

- `ai_stock/performance.py:182` `summarize()`：按五档评级统计胜率、跑赢比例、方向正确率、
  `rating_monotonicity()`（评级越高是否收益越高），有 `MIN_MEANINGFUL_SAMPLE` 小样本保护。
  但其调用点只有 `cli/main.py:1474`，数据源是 `TradingMemoryLog`（研究/评级 agent 的记忆日志），
  **不覆盖量化交易链路**。
- `ai_stock/quant/evolution.py:60` `backtest_stop_policy()` + `search_params()` + `evolve_stop_loss()`：
  严格的时序三段分割（训练/验证/样本外），只回测“止损/时间止损”策略。
  除自身外**没有调用方**（无 CLI、无调度、无后台任务），属已实现未接线。

**缺口**：交易链路的三个问题没有数据出口——
① 买入决策漏斗（`reject_stage` 已结构化，但没有汇总报表）；
② 技术信号的胜率/盈亏比（哪个形态赚钱、哪个形态是负期望）；
③ 卖出原因分布与卖出后的表现（止损后 5 日涨回的比例，用来判断止损位是否过紧）。

**需求**

1. 新增 `scripts/trade_attribution.py`（只读，不改生产数据），按周输出：
   - 漏斗：`buy_scan → flow → tech_signal → catalyst → second_verification → pre_trade_check → filled`
     各环节通过数与 `reject_stage` 分布；
   - 信号：按 `entry_reason` 归类（四种技术形态 + breakout 旁路）统计笔数、胜率、平均盈亏、平均持有天数；
   - 卖出：按卖出原因（stoploss / time_stop / major_bear / partial）统计，并给出卖出后 5 日涨跌幅。
2. 把 `evolution.py` 接上调度（建议周频、仅交易日历收盘后），先在“只计算不写配置”模式跑通，
   输出的最优参数进周报，人工确认后再改 `config.py`。
3. 明确边界：**不允许**脚本自动改写 `config.py` 里的交易参数（进化结果只作为建议）。

**验收**

1. 脚本在 `backend` 目录 + `.env` 下可运行（沿用 `scripts/news_board_refresh.py` 的运行约定），
   输出 markdown 报告到 stdout 或指定路径。
2. 给定测试库中的 N 笔成交，漏斗各环节数字与 `quant_agent_decision_log` 手工统计一致。
3. 卖出后表现统计能处理“卖出后数据不足 5 日”的情况（标注 `insufficient`，不计入均值）。

---

## 5. 待确认事项（不按 bug 处理）

以下三条是走查中发现的**行为与配置语义存疑**之处，需要业务确认后再决定是否立需求，避免过度解读：

1. **`TIME_STOP_DAYS_BY_STAGE` 中 `TAIL: 0` 被 `or` 吞掉**
   `hold.py:143-145`：`int(TIME_STOP_DAYS_BY_STAGE.get(stage, ...)) or TIME_STOP_TRADING_DAYS`，
   即 `TAIL=0` 会回退成 5 天。若 `TAIL` 的本意是“立即清仓”，这是缺陷；若本意只是“鱼尾不进池”
   （观察期在 `OBSERVE_EXPIRE_DAYS_BY_STAGE` 另有配置），则当前行为无害。**需确认 TAIL 的时间止损语义**。

2. **建仓路径没有“买入后单票占比”校验**
   `buy.py:1189` 只按“可用资金 × 20% × 系数”定量，`pre_trade_check` 也只校验同一比例
   （`risk_control.py:195-205`），没有校验“成交后该票占总资产比例”。
   实际约束由 `POSITION_RATIO_HARD_CAP=24%` 与 `MAX_HOLDING_COUNT=3` 间接成立，**当前不可利用**；
   但加仓路径（`hold.py:284-289`）用的是 `SINGLE_POSITION_MAX_PCT` 余量口径，两处不对称。
   建议统一为“成交后占比”校验，属口径统一，不是紧急缺陷。

3. **跌停卖出的业务语义**
   见 FR-13。模拟盘当前是“拒单”，若产品认为“跌停本来就卖不掉、拒单是对的”，则 FR-13 可降级为
   文档补充（在 `docs/TECHNICAL.md` 写明模型假设），不必改代码。

---

## 6. 实施顺序与验收流程

**阶段一（P0，建议 1~2 个迭代内完成）**

1. FR-01 → FR-03 → FR-02 → FR-04（决策）。
   FR-01 必须先做：它会影响 FR-03 之后的冻结行为观察；若先做 FR-03 再做 FR-01，会出现两次行为跳变，难以归因。
2. 每项必须带单测，运行方式：`cd backend && .venv/bin/python -m pytest tests/ -k "risk_control or hold"`
   （根目录 `.venv` 无 pytest，注意别用错环境）。
3. 上线后观察 5 个交易日：冻结次数、卖单拒单率、`rr_missing` 占比，三项都要在周报里出现。

**阶段二（P1）**

FR-06（先定口径）→ FR-05 → FR-07 → FR-08 → FR-09（上实盘前必须完成）。

**阶段三（P2）**

FR-11（改动最小、收益明确，可提前）→ FR-10 → FR-12 → FR-13 → FR-14。

**通用验收要求**

- 每条需求合入前需说明“改动前后的行为差异”，并给出至少一个反例测试（即修复前会失败、修复后通过的用例）。
- 涉及止损/止盈/仓位的改动（FR-05、FR-06、FR-07），必须先在模拟盘跑满 10 个交易日对比新旧行为的差异报告，
  不得直接上生产。
- 所有新增配置项必须有默认值且可回滚，不允许出现“改了就必须改回去才能跑”的硬耦合。

---

## 附录 A：证据索引

| 编号 | 证据位置 |
|---|---|
| FR-01 | `risk_control.py:61-65`、`:117-119`、`:377-379`；`buy.py:1195-1203` |
| FR-02 | `hold.py:500-506`；`risk.py:80-86`；`broker.py:344-346`；`risk_control.py:130-136`、`:165-167` |
| FR-03 | `risk_control.py:71`、`:120-124`；`db_ops.py:657-679` |
| FR-04 | `buy.py:1100-1104`、`:129-135`、`:119`；`config.py:85` |
| FR-05 | `buy.py:1340`、`:1347`；`hold.py:268-277`、`:624`；`config.py:264-265` |
| FR-06 | `data_service.py:536-559`；`hold.py:113`、`:125`、`:168-181`；`evolution.py:60-70` |
| FR-07 | `hold.py:364-372`、`:389-421`；`config.py:172` |
| FR-08 | `market_regime.py:76-98`；`risk_control.py:279-301` |
| FR-09 | `broker.py:508-525`、`:564-584`；`buy.py:1262`；`hold.py:500`、`:567`；`risk.py:80` |
| FR-10 | `buy.py:294-320` |
| FR-11 | `news_impact.py:842-853`；对照 `risk_control.py:34-39` |
| FR-12 | `theme_registry.py:321`、`:38-48` |
| FR-13 | `broker.py:344-346` |
| FR-14 | `performance.py:182`；`cli/main.py:1474`；`evolution.py:60`、`:171`、`:222` |

## 附录 B：本次未覆盖的范围

- 新闻主题的关键词覆盖与榜单排序权重（属 `THEME_TAXONOMY_DESIGN.md` 范畴，已有独立审计脚本）。
- 数据源可用性（东财 push2 被拒、熔断机制）已在 `QUANT_FIX_PLAN.md` 中处理。
- 前端展示与 API 层：本次只涉及后端行为，前端如需配合（如显示 `daily_pnl_total`、`pending_exit` 状态），
  作为各需求的附属任务另行排期。
