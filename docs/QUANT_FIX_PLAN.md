# 量化板块「从不下单」故障复盘与优化方案

> 采集时间：2026-10-17 17:00 ｜ 目标机：118.25.48.120（腾讯云上海）
> 部署：`/deploy/AI_stock`，systemd `ai-stock-backend.service`，uvicorn 127.0.0.1:8000，运行用户 www-data
> 数据源：`/deploy/AI_stock/backend/data/aistock.db`（quant 与 backend 共用）

---

## 一、故障复盘：两层断点叠加，形成「永远不下单」

```
【上游断点 ①】东财 push2 板块接口对该服务器 IP 拒绝连接
        ↓  get_all_board_fund_flow() 返回 0 条
        ↓  IndustryScanAgent._collect_candidates() = []
        ↓  industry_scan 空转 908/967 = 94%
        ↓  StockSelectionAgent 无候选行业 → Level 3 降级「续持旧池」598 次
        ↓  自选池老化：removed 127 / active 8，无新鲜标的补充
        ↓  只有 8 只老票反复走买入流程

【末端闸门 ②】global_trade_enable = '0'
        ↓  29 笔走到 position_open 的单，全部被 pre_trade_check 拒绝
        ↓  quant_trade_log = 0 —— 从未成交
```

### 证据 1：末端闸门（直接原因）

```
quant_system_config.global_trade_enable = 0
quant_agent_decision_log: position_open → open_risk_rejected × 29
    reason 全部为：全局交易开关关闭 (global_trade_enable=0)
quant_trade_log: 0 条
```

拒绝日期：9/03(8)、9/04(8)、9/07(1)、9/09(2)、9/15(5)、9/22(2)、9/24(1)、10/01(2)。

拦截代码 `risk_control.py:73`：

```python
if db_ops.get_config_value("global_trade_enable", "0") != "1":
    return CheckResult(False, "全局交易开关关闭 (global_trade_enable=0)")
```

**为什么代码里默认值是 1 却不生效** —— 配置种子无版本化：

| 提交 | `SYSTEM_CONFIG_SEED["global_trade_enable"]` |
|---|---|
| `95c8361 新增量化功能` | `"0"` |
| `1f80d73 优化` | 改为 `"1"` |

而 `service._seed_system_config()` 的语义是「已存在的键一律不覆盖」。该库在 9 月按 `"0"` 初始化，之后代码把默认值改成 `1`，**新默认值永远不会写进老库**。同时 `main.py:144`、`core/config.py:67`、`service.py:48` 的注释与日志都还写着「默认 0」，与种子值矛盾，掩盖了这个问题。

### 证据 2：上游断点（根因中的根因）

现场实测同一请求，服务器与本地结果完全不同：

| 请求 | 本地（南京家宽） | 服务器（腾讯云上海） |
|---|---|---|
| `push2.eastmoney.com/api/qt/clist/get?fs=m:90+t:2` | **200**，返回 496 个板块 | **000**，连接被关闭 |
| `push2delay.eastmoney.com` 同路径 | — | **000** |
| `82.push2.eastmoney.com` 同路径 | — | **000** |
| `datacenter-web.eastmoney.com/api/data/v1/get` | — | **200** |
| `push2his.eastmoney.com` 日 K 线 | — | **200**（253B） |
| `quote.eastmoney.com` | — | **200** |
| `get_realtime_quote('600150')` | — | **正常**返回 35.62 |

结论：**故障被精确限定在 `push2*.eastmoney.com` 的 `clist/get`（板块列表）接口**。TLS 握手正常、TCP 443 可连、同域其他路径可用，说明是应用层（WAF / 反爬 / IP 黑名单）而非网络阻断。合理的推测是：程序 7×24 每 10 分钟轮询行业板块 + 概念板块 + 成分股，触发反爬策略导致该 IP 在 clist 集群被拉黑。

> 注意 `_EM_PUSH2_HOSTS` 的「主域被断连时回退延迟域」机制在这里完全无效 —— 两个域同时被封，回退等于没回退，且 `DataSourceCircuit` 熔断器记录的是「失败」，没有区分「被封禁」与「临时超时」，无法触发人工告警。

### 证据 3：漏斗本身很窄（不是故障，是设计问题）

701 条 buy flow 的终点分布：

| 终点步骤 | 数量 | 占比 | 含义 |
|---|---|---|---|
| `catalyst_event` | 529 | 75.5% | 技术信号未过 且 无强催化+利空消失 |
| `second_verification` | 71 | 10.1% | LLM 二次验证拒绝（63 次） |
| `logic_collapse_check` | 69 | 9.8% | 逻辑崩塌/鱼尾，移出自选池 |
| `position_open` | 29 | 4.1% | 走到末步，被开关拦下 |
| `tech_signal` | 3 | 0.4% | 指标不可用 |

决策计数：`tech_no_signal` 544 / `tech_signal` 85（通过率 **13.5%**）。

**当前池内 8 只票的技术信号实测（2026-10-16 收盘数据）：**

```
688002  boll=False macd=False gap=False breakout=False
603087  boll=False macd=False gap=False breakout=False
600150  boll=False macd=False gap=False breakout=False
002179  boll=False macd=False gap=False breakout=False
002414  boll=False macd=False gap=False breakout=False
600760  boll=False macd=False gap=False breakout=False
301699  NO_DATA（取不到 K 线）
688117  boll=False macd=False gap=False breakout=False
```

**0 / 7 命中**。三个技术信号 + 一条突破通道，一个都没触发。

---

## 二、修改方案

### P0-1　修复板块数据源（最重要，影响面最大）

**为什么改**
`industry_scan` 是整个选股链路的第一道闸门，94% 空转意味着选股系统事实上处于「停机」状态，只是没有报错。它是「池子只剩下 8 只老票」的直接原因，也是买入漏斗只有 4% 的根本原因。

**怎么改**（按顺序做，前两步当天可完成）

1. **确认封禁性质**：换一台不同出口 IP 的机器（或临时挂代理）请求同一 URL。
   - 若换 IP 即恢复 → 是 IP 级封禁，走 (3)(4)
   - 若换 IP 仍 000 → 是请求特征识别，走 (2)
2. **请求侧伪装与节流**（低成本，先做）
   - 补齐 `User-Agent` + `Referer: https://quote.eastmoney.com/` + `Accept-Encoding`
   - 全局限流：板块列表 ≥5 分钟一次、成分股 ≥10 分钟一次，进程内单例缓存复用
   - 失败退避：`3 次失败 → 指数退避（5/15/60min）`，不要每 10 分钟硬撞一次（硬撞只会加重封禁）
3. **增加第二数据源**（治本）
   - 首选 **通达信 mootdx（TCP 7709）**：不走 HTTPS、不受反爬影响，`docs/DEPLOYMENT.md` 已把它列为环境依赖，但行业板块链路没有用它
   - 次选 `datacenter-web.eastmoney.com`（实测 200 可用），用它取板块日线快照而非实时 clist
4. **出口隔离**（若确认 IP 封禁且短期无法解封）
   - 给数据采集加独立出口（轻量代理 / 同区域另一台低配 CVM 做数据代理），与 Web 服务分离

**改后效果**
- `industry_scan` 非空率：6% → **>90%**
- `select_done`（真正选出新票）：5.7% → **>80%**，不再长期靠「续持旧池」
- 自选池日更新标的质量与数量恢复，`removed` 不再单边增长

**预计指标**
- 每个选股槽位（7:00/12:00/18:00）稳定产出 3~9 只新候选
- 池内标的平均「入池天数」从当前老化状态回到 7 天观察期内的正常分布

---

### P0-2　打开全局交易开关

**为什么改**
这是「一笔都没成交」的直接原因。开关是模拟盘的唯一总闸，关着的时候，即时上面所有环节都完美，也不会产生任何成交。

**怎么改**（两条腿，都要）

1. **运维即时处置**（不重启生效，`pre_trade_check` 每次实时读配置）：
   ```bash
   sudo /deploy/AI_stock/backend/.venv/bin/python3.12 -c "
   import sqlite3
   c=sqlite3.connect('/deploy/AI_stock/backend/data/aistock.db')
   c.execute(\"update quant_system_config set value='1' where key='global_trade_enable'\")
   c.commit(); print(c.execute(\"select value from quant_system_config where key='global_trade_enable'\").fetchone())
   "
   ```
2. **代码侧修根因**：`_seed_system_config()` 增加 `config_schema_version` 字段 + 显式迁移表，让「默认值变更」能够下发到已部署库。否则下次再改任何默认值，同样的问题会重演。

**改后效果 / 预计指标**
- 历史通过率 29/701 ≈ 4.1%，按每日 20+ 条 buy flow 估算，**理论每周 4~6 笔成交**（受 `MAX_HOLDING_COUNT=3` 上限约束，前 3 笔成交后买入扫描会自动停）
- ⚠️ **必须与 P0-1 一起做**：若只开开关不修数据源，池子仍是 8 只老票且信号 0/7 命中，实测 10/02 之后已无任何 flow 走到 `position_open`，开关打开也仍然是 0 成交

---

### P1-1　修正仓位系数与风控上限的自相矛盾（确定性 Bug）

**为什么改**
`position_open` 按下式计算预算：

```python
budget = available_cash * BUY_POSITION_RATIO * coef   # coef 可为 1.2
```

而 `pre_trade_check:142` 用「可用资金 × 20%」作为硬上限校验：

```python
budget = snap.get("available_cash", 0.0) * BUY_POSITION_RATIO
if est_amount > budget: return CheckResult(False, "超过可用资金 20%")
```

→ `dyn_confidence ≥ 0.8 且 stage=BODY`（仓位系数 1.2 = 24%）的标的，**下单金额必然超过 20% 上限，必然被拒**。同时 24% 还会越过 `max_order_value=200000`（等于 100 万本金的 20%）。

**这是「置信度最高的票反而买不进」的系统性缺陷**，恰好与「宁缺毋滥」的产品意图相反。

当前池内 dyn 最高 0.74（`600150` 0.742），所以该 Bug 尚未触发 —— 属**潜伏雷**，一旦出现高置信标的就会立即踩中。

**怎么改**
二选一并保持单一事实来源：
- 方案 A（推荐）：`pre_trade_check` 接受 `position_ratio` 参数，上限校验用「实际下单比例」，而不是硬编码 20%
- 方案 B：把 `POS_COEF_HIGH` 降到 1.0，取消动态加仓系数（简单但丢了动态仓位能力）

同时把 `max_order_value` 从 200000 调到 250000 或按 `initial_cash × 25%` 动态计算，避免与仓位公式打架。

**预计效果**：高置信标的建仓成功率从 0% → 100%（其余校验通过时）。

---

### P1-2　重建技术信号体系（当前 0/7 命中）

**为什么改**
三个信号全部是**均值回归/抄底型**：
- `boll_mid_shrink_stop`：连续 2 根缩量 + 未创新低 + 回踩中轨
- `macd_divergence`：价格新低 + MACD 未新低 + RSI<35 或 KDJ 低位金叉
- `gap_pullback_shrink`：回踩未封闭缺口 + 缩量

外加一条 `breakout` 通道（放量跳空 / 突破前高），但它被 `dyn_confidence ≥ 0.7` 且 `stage ∈ HEAD/BODY` 双重门控 —— 而池内 dyn 最高 0.74，几乎只有极少数标的能进这个通道。

结果：**系统只会「等回调抄底」，没有「趋势跟随进场」**。在行业主线启动、连续放量上涨的行情里，它永远等不到缩量回踩，于是永不下单。这正是 0/7 命中、tech 通过率长期 13% 的结构性原因。

**怎么改**
1. **补一条趋势入场通道**：`close > MA20 且 MA20 上翘 且 量能 > 1.2×20日均量 且 RSI(14) ∈ [50,75]`（追强不追妖）
2. **放宽 breakout 通道门控**：`CONF_BREAKOUT_THRESHOLD` 0.7 → 0.55，或改为「池内 dyn 排名前 30%」的相对门槛（绝对阈值与 dyn 的实际分布不匹配 —— 实测池内 dyn 中位数只有 0.42）
3. **`_macd_divergence` 的「新低」口径修正**：`close[-1] <= min(close[:-1])` 只在 5 根序列里比较，等于只跟最近 4 天比，不是真正的背离。应改用 20~30 根窗口
4. **`close_series` 从 `tail(5)` 扩到 `tail(30)`**：`high_series/low_series` 已经是 `tail(21)`，序列长度不一致，容易写出「用 5 根收盘价判断 11 根突破」这类隐性 Bug

**预计效果**：tech 通过率 13% → **25%~35%**，且在单边上涨行情中不再完全空仓。

---

### P1-3　调整二级 LLM 关卡的拒绝率

**为什么改**
`second_verification` 63 次拒绝，要求 `passed ∧ major_bear_resolved` **同时成立**。`major_bear_resolved` 是一个很苛刻的布尔量 —— 只要入选时的利空清单里还有一条没消除，就直接拒绝，哪怕其他条件极佳。这构成「双保险变成双闸门」。

**怎么改**
- 把 `major_bear_resolved` 从「硬性 AND」改为「评分项」：未消除但风险等级低（LLM 打标 `low/medium/high`）时放行，仅 high 级拒绝
- 拒绝原因结构化落库（当前只存文本 reason），便于事后统计「到底卡在哪个子条件」

**预计效果**：末步到达率从 4.1% → **8%~12%**。

---

### P1-4　自选池清理策略回调

**为什么改**
`removed 127` vs `active 8`，清理官 + 崩塌检测每天移除 3~10 只。叠加 P0-1 导致「进得少、出得多」，池子必然枯竭。

**怎么改**
- `CONF_KICK_THRESHOLD` 0.3 → 0.25（当前 dyn 中位数 0.42，0.3 的踢出线相对偏紧）
- `logic_collapse_check` 的 LLM 崩塌判定加「连续 2 次确认才移除」，避免单次 LLM 采样波动误杀
- `OBSERVE_EXPIRE_TRADING_DAYS` 7 天对主线题材偏短，可分层：HEAD 阶段 10 天、BODY 7 天、TAIL 立即出

---

## 三、架构层面可优化点（架构师视角）

### A1　数据层：单一供应商 + 无「健康度矩阵」

**问题**：全部板块数据押在 `push2` 一家。且 `DataSourceCircuit` 熔断器只记录失败次数，无法区分「临时超时 / 被反爬封禁 / 接口下线」三种完全不同的故障，导致封禁持续数周都无人知晓（本次即如此）。

**建议**
- 建立数据源健康矩阵：`(源, 接口, 成功率, P95延迟, 最近成功时间)`，成功率跌破 50% 持续 30 分钟即告警
- 关键接口（板块列表、个股行情、日 K）各自配置 2 个独立供应商，主备自动切换并记录切换事件
- 采集出口与 Web 出口分离，避免「采集被封 → 影响整站」

### A2　配置层：种子值无版本化（本次故障的制度性根因）

**问题**：`_seed_system_config` 只补缺、不覆盖。任何默认值变更都到不了已部署环境。

**建议**
- `quant_system_config` 增加 `schema_version`，建迁移表 `config_migrations: (version, key, old, new)`
- 启动时按版本号顺序执行迁移，**只迁移「仍是旧默认值」的键**（用户手工改过的值不动）
- 同时在 CI 加一条断言：`SYSTEM_CONFIG_SEED` 的变更必须附带迁移脚本

### A3　可观测性：告警只落库，等于没有告警

**问题**：`ops_monitor.emit()` 只写 `logger` + `quant_agent_decision_log`。这台机器上没有任何外部通道（无 webhook / 邮件 / 企业微信）。本次「连续 6 周 0 成交」「行业扫描 94% 空转」都属于「严重但不报错」的静默故障，现有机制完全捕捉不到。

**建议**
- 加最小可用的外部告警通道（企业微信机器人 / 邮件任选其一）
- 增加**业务级 SLO 告警**，而不是只报异常：
  - `industry_scan` 连续 3 次空 → WARNING
  - `quant_trade_log` 连续 5 个交易日无新增 → CRITICAL
  - `select_fallback` 占比 > 50% → WARNING
- 接 Prometheus `/metrics`，输出 flow 漏斗、队列深度、LLM 调用耗时/失败率

### A4　状态机：去重表在内存 + 幂等 TTL 仅 1 小时

**问题**：`Orchestrator._dedupe` 是进程内 `OrderedDict`，重启即失效；`IDEMPOTENT_TTL=3600`，超过 1 小时的重复事件会被重新执行。当前单机 + 单 worker 侥幸没问题，一旦多 worker 或重启补跑就会出问题。

**建议**：去重键落库（`processed_events(flow_id, step, ts)` + TTL 清理），幂等 TTL 覆盖最长流程时长（`FLOW_TIMEOUTS['selection']=3600`，两者相等 → 边界上必然失效，建议 ≥ 2×）。

### A5　扩展性：单进程内存任务表

**问题**：`AnalysisTaskManager` 在进程内存，`uvicorn --workers 1` 是硬约束（部署文档 B5 已承认）。当前业务量小没问题，但这是「永久单点」。

**建议**：中期把任务状态迁到 DB/MQ（`quant_task` 表已有），让 consumer 无状态化，才能真正水平扩展。短期至少给 systemd 加 `Restart=always` + 启动自检（`recover_on_startup` 已有，需确认真的被调用）。

### A6　存储：SQLite 单文件 50MB 且部分表无清理

**问题**：`quant_agent_decision_log` 已 20,398 行，`maintenance_cleanup` 只清 `quant_cache_data` 和新闻向量，**不清决策日志**；`quant_flow_state` 1,668 行且每个 30 分钟窗口每只票一条，只增不减。50MB 增长虽慢，但无归档策略等于迟早要做一次停机迁移。

**建议**
- 决策日志按 90 天分区归档（或直接 add 清理任务）
- `quant_flow_state` 终态超过 30 天归档
- 开 WAL 模式（`PRAGMA journal_mode=WAL`）降低读写竞争 —— uvicorn 线程 + consumer 线程并发读写同一文件

### A7　成本：选股链路 LLM 调用存在大量无效开销

**问题**：`select_fallback` 598 次说明「跑完整套 LLM 流程后发现无候选行业」。即：`industry_scan` 在数据为空时**仍然消耗了 LLM 生命周期定位的调用预算**（虽然有 `if not self.llm.available` 的短路，但候选为空时是更早短路，这一点还好）。真正浪费的是 `macro_event` / `theme_radar` / `limit_up_monitor` 三步在每个槽位都跑满 —— `radar_scan_empty` 918 次、`scan_empty` 908 次，这些空跑本该在数据健康检查失败时直接跳过。

**建议**：流程入口加「数据健康预检」，主数据源不可用时直接降级为「跳过本槽位 + 告警」，而不是跑完整条链路再空手而归。

---

## 四、交易功能可优化点（交易师视角）

### T1　仓位管理：固定比例，且资金利用率被压得很低

当前：单笔 = 可用资金 × 20%（×系数），最多 3 只 → **最高只用 60%~72% 资金**，长期 28%+ 空转。

更关键的是：**用「可用资金比例」而非「波动率」定仓位**。同样 20%，买中国船舶（年化波动 40%）和买甘李药业（波动 25%）承担的风险完全不同。

**建议**
- 改为**波动率目标仓位**：`qty = (总资产 × 单笔风险预算 1%) / (2×ATR%)`，让每笔最大亏损趋同
- 保留 3 只上限，但引入「风险预算总闸」：组合层面回撤触及 -6% 时仓位系数降半

### T2　无行业集中度约束（当前池子已经很危险）

当前 8 只 `active` 中，**中国船舶、中航沈飞、中航光电、高德红外、睿创微纳 5 只是军工/军工电子产业链**（占 62.5%）。在只有 3 个持仓名额、且 `handle_buy_scan` 按池子顺序取前 3 只的逻辑下，**极可能一次性买成「全仓军工」**——这不是分散，是加杠杆押注单一 beta。

**建议**
- `pre_trade_check` 增加硬约束：同一行业（申万一级）持仓数 ≤ 1，或市值占比 ≤ 40%
- 这是**优先级仅次于 P0 的交易级缺陷**，因为一旦开闸就会立刻暴露

### T3　无大盘择时

全代码库没有任何指数趋势过滤（无沪深 300 均线、无市场宽度、无涨跌家数）。熊市里信号同样会触发，系统会持续抄底。

**建议**：加一层市场状态门（`沪深300 > MA20` 且 `MA20 上翘` 才允许新建仓；否则只减不加）。这一条对最大回撤的改善，通常比调止损参数更显著。

### T4　止损 5% + 两根 K 线确认，实际滑点不可控

`STOP_LOSS_PCT=0.05`，但要求**连续两根 K 线收盘**都在成本 -5% 以下才触发。这在急跌行情里意味着实际亏损可达 8%~12%。

**建议**：改为 `min(成本×0.95, 成本 - 2×ATR)`，并保留「盘中触及即预警、收盘确认即执行」的两段式，但把 2 根改为 1 根 + 盘中破位预警。

### T5　时间止损 5 日 3% 对主线股过早

`TIME_STOP_TRADING_DAYS=5`、`TIME_STOP_MIN_GAIN=0.03`：5 个交易日没涨 3% 就清仓。对题材主升浪前的横盘蓄势极其不利，会被反复"洗"出去。

**建议**：与 `stage_batch` 联动 —— HEAD 阶段给 10 日、BODY 给 7 日，且用「相对大盘超额收益」而非绝对涨幅衡量。

### T6　信号体系缺动量维度（与 P1-2 同源）

三个信号全是均值回归。一个只有「抄底」没有「追强」的系统，在结构性行情里会长期空仓，在震荡市里会被反复止损。**技术面应至少包含「趋势 / 动量 / 波动率」三个正交维度**，而不是三个高度相关的回调形态。

### T7　缺少基准与归因

没有任何「策略 vs 沪深 300」的对比，也没有按行业/信号类型拆解的收益归因。`ops_monitor.generate_daily_report` 只报账户数字，无法回答「这个月是靠什么赚/亏的」。

**建议**：日报增加「超额收益（vs 沪深300）」「胜率 / 盈亏比 / 平均持仓天数」「按行业归因」三项。没有这三个数，后续所有参数调优都是盲调。

### T8　30 分钟扫描粒度对 A 股偏粗

`BUY_SCAN_INTERVAL_MINUTES=30`：一天只有 8 次机会窗口。A 股很多信号的有效窗口就是 15~30 分钟（尤其缩量回踩），错过就没了。

**建议**：买入扫描降到 10 分钟（与持仓扫描同频），或改为「信号触发即入队」的事件驱动 —— 由行情轮询线程在命中形态时直接投递 buy flow，而不是等下一个 30 分钟槽位。

### T9　缺少 ST / 退市风险 / 停牌过滤

`pre_trade_check` 有涨停不买、跌停不卖、T+1、白名单，但**没有 ST/*ST 剔除、没有停牌过滤、没有次新股（上市 < 60 日）过滤**。这些在 A 股是硬性风控项。

---

## 五、优先级与预期收益

| 优先级 | 事项 | 工作量 | 预期效果 |
|---|---|---|---|
| **P0-1** | 板块数据源恢复 + 第二数据源 | 1~2 天 | 选股从「实质停机」恢复，池子重新有新鲜血液 |
| **P0-2** | 打开 `global_trade_enable` + 配置迁移机制 | 0.5 天 | 立即恢复下单能力；杜绝同类配置漂移 |
| **P1-1** | 仓位系数 vs 风控上限矛盾 | 0.5 天 | 消除「高置信标必被拒」的潜伏雷 |
| **P1-2** | 补趋势入场通道 + 修正指标口径 | 2~3 天 | tech 通过率 13% → 25~35% |
| **P1-3** | 二次验证关卡软化 | 1 天 | 末步到达率 4.1% → 8~12% |
| **P1-4** | 池子清理策略回调 | 0.5 天 | 池子不再单边枯竭 |
| **T2** | 行业集中度硬约束 | 0.5 天 | 避免「3 只全仓军工」的组合性风险 |
| **T3** | 大盘择时门 | 1 天 | 显著改善最大回撤 |
| **T4/T5** | ATR 止损 + 分层时间止损 | 1 天 | 降低实际滑点损失、减少被洗出 |
| **A3** | 业务级 SLO 告警 | 1 天 | 同类静默故障不再「六周无人知」 |
| **A4/A6/A7** | 状态落库 / 存储归档 / 健康预检 | 3~5 天 | 为多 worker 与长期运行铺路 |

### 关键结论

- **只做 P0-2（打开开关）→ 仍然是 0 成交**。因为 10/02 之后已无 flow 能走到末步，技术信号实测 0/7。
- **只做 P0-1（修数据源）→ 池子恢复但依然 0 成交**，因为开关还是关的。
- **P0-1 + P0-2 + P1-1 一起做**，才是「能下单且不会踩雷」的最小可用集合。

---

## 六、改完怎么验证

```bash
# 1. 数据源恢复
python3 -c "from ai_stock.quant.data_service import get_data_service; \
  print(len(get_data_service().get_all_industries()))"     # 期望 > 300

# 2. 选股不再降级（观察 1~2 个槽位后）
#    select_done / (select_done+select_empty+select_fallback) > 80%

# 3. 漏斗恢复（观察 3~5 个交易日）
#    position_open 到达数 / buy flow 总数  > 8%

# 4. 真实成交
#    quant_trade_log 出现新行；quant_stock_pool_holding 出现持仓
```

对应 SQL 见 `docs/DEPLOYMENT.md` §8 监控与健康检查，建议把上述 4 项直接做成日报指标。

---

## 七、落地情况（本地实施记录）

本章记录方案在本地代码库的实际落地状态，便于后续对账与回归。

### 7.1 已落地

| 条目 | 落地位置 | 关键实现 |
|---|---|---|
| P0-1 | `dataflows/pipeline_data.py`、`dataflows/data_health.py`、`quant/data_service.py` | 板块列表「东财 push2 → 新浪」双源回退；每源独立健康矩阵 + 指数退避（5/15/60 min）；空结果按失败计；进程内最短采样间隔；全源失败用上次快照兜底；`get_board_constituents` 支持 `new_`/`gn_` 前缀走新浪 |
| P0-2 | `quant/config.py`、`quant/service.py` | `SYSTEM_CONFIG_SEED` 默认 `global_trade_enable=1`；新增 `CONFIG_SCHEMA_VERSION` + `CONFIG_MIGRATIONS` 版本化迁移（只迁移「仍是旧默认值」的键，人工设置保留） |
| P1-1 | `quant/config.py`、`quant/risk_control.py`、`agents/buy.py`、`agents/hold.py` | `POSITION_RATIO_HARD_CAP = 20% × 1.2`；`pre_trade_check(..., position_ratio=)` 用实际下单比例校验上限（1‰ 容差）；`max_order_value` 200k → 250k |
| P1-2 | `agents/buy.py`、`quant/data_service.py` | 新增第 4 通道「趋势动量」（站上 MA20 + MA20 上翘 + 放量 1.2× + RSI∈[50,75]）；突破通道 `0.70` 绝对阈值改为 `0.55 或 dyn 排名前 30%`；MACD 背离改 25 根窗口；指标序列长度 5 → 30（`ma20/ma20_slope/volume_ratio_20/atr_pct`） |
| P1-3 | `agents/buy.py` | `VerificationReport.bear_risk_level`（low/medium/high）替代「major_bear_resolved 必须为真」的硬 AND；新增 `reject_stage` 结构化字段 |
| P1-4 | `agents/buy.py`、`quant/db_ops.py`、`quant/config.py` | 清理官 / 崩塌检测均改为「连续 2 轮确认」（`kick_pending` → `kicked`），鱼尾与年线破位硬规则仍立即移除；`CONF_KICK_THRESHOLD` 0.3 → 0.25；观察期按 `OBSERVE_EXPIRE_DAYS_BY_STAGE`（HEAD 10 / BODY 7 / TAIL 3）分层 |
| T1 | `agents/buy.py`、`quant/config.py` | 波动率目标仓位 `qty = 总资产×1% ÷ (2×ATR)`，与资金上限 `min()` 双约束；组合回撤 ≥6% 时仓位系数打对折；`plan.sizing` 留痕（`qty_by_cash` / `qty_by_risk` / `risk_budget` / `coef`） |
| T2 | `quant/risk_control.py`、`quant/db_models.py`、`quant/db.py` | 同行业持仓数 ≤ 1（`INDUSTRY_CONCENTRATION_MAX`）；持仓表新增 `industry` 列 + 幂等迁移 |
| T3 | `quant/market_regime.py`、`quant/risk_control.py` | 沪深 300 vs MA20 择时门，`risk_off` 拒绝建仓，`unknown`（数据缺失）放行；30 分钟缓存 |
| T4/T5 | `agents/buy.py`、`agents/hold.py` | 止损位 `min(成本×0.95, 成本-2×ATR)`（下限 -15%）；确认根数 2 → 1 并新增盘中破位预警；时间止损按阶段 HEAD 10 日 / BODY 7 日 / TAIL 0 |
| T6 | 同 P1-2 | 技术面从「三个均值回归形态」变为「均值回归 + 趋势动量」两族 |
| T7 | `quant/ops_monitor.py` | 日报新增绩效（胜率/盈亏比/平均持仓天数/已实现盈亏）、行业归因、沪深 300 基准与超额收益 |
| T8 | `quant/config.py` | `BUY_SCAN_INTERVAL_MINUTES` 30 → 10（一天机会窗口 8 → 24） |
| T9 | `quant/risk_control.py` | ST/退市、停牌（price ≤ 0）、次新股（K 线 < 60 根）三项硬过滤 |
| A1/A7 | `dataflows/data_health.py`、`agents/selection.py` | 健康矩阵快照 + 降级源识别；选股入口健康预检，板块数据为空直接跳过槽位并告警 |
| A3 | `quant/ops_monitor.py`、`quant/scheduler.py` | `check_business_slo()` 四项巡检（行业扫描连续空 / 选股降级占比 / 连续 N 日无成交 / 数据源成功率）+ 企业微信 webhook 外发，每 30 分钟一轮 |
| A4 | `quant/orchestrator.py`、`quant/config.py` | `EVENT_DEDUPE_SECONDS` 600 → 7200（≥2× 最长流程）；事件去重落库（`evt_seen:` 前缀），重启/多 worker 仍生效 |
| A6 | `quant/db_ops.py`、`quant/scheduler.py` | 决策日志保留 90 天、终态流程保留 30 天（running 保留）；WAL 原有 |
| A5 | `backend/app/services/analysis_state.py`(新)、`analysis_service.py`、`app/models/analysis_task.py`、`app/main.py`、`app/core/database.py` | 任务状态表内化：进度/结果/心跳/控制指令落 `analysis_tasks`；任意 worker 可读状态与结果、可下发 pause/resume/stop；孤儿按**心跳超时**回收（不再启动即冻结所有 running），`uvicorn --workers N` 不再互杀 |

### 7.2 新增配置项（`ai_stock/quant/config.py`）

```
POSITION_RATIO_HARD_CAP / OBSERVE_EXPIRE_DAYS_BY_STAGE / INDUSTRY_CONCENTRATION_MAX
CONF_BREAKOUT_TOP_PCT / KICK_CONFIRM_ROUNDS / MACD_DIVERGENCE_WINDOW
TREND_MA_WINDOW / TREND_MA_SLOPE_MIN / TREND_VOLUME_RATIO / TREND_RSI_MIN / TREND_RSI_MAX
VOL_TARGET_SIZING_ENABLED / RISK_BUDGET_PER_TRADE / PORTFOLIO_DD_COEF_THRESHOLD / PORTFOLIO_DD_COEF_FACTOR
STOP_LOSS_ATR_MULT / STOP_LOSS_CONFIRM_BARS / TIME_STOP_DAYS_BY_STAGE
MARKET_TIMING_ENABLED / MARKET_INDEX_CODE / MARKET_MA_WINDOW / MARKET_REGIME_TTL / NEW_LISTING_MIN_BARS
DATA_SOURCE_FAIL_THRESHOLD / DATA_SOURCE_BACKOFF_STEPS / DATA_HEALTH_SUCCESS_RATE_ALERT / BOARD_FLOW_MIN_INTERVAL
ALERT_WEBHOOK_URL / SLO_SCAN_EMPTY_STREAK / SLO_NO_TRADE_DAYS / SLO_SELECT_FALLBACK_RATIO
DECISION_LOG_RETENTION_DAYS / FLOW_STATE_RETENTION_DAYS
CONFIG_SCHEMA_KEY / CONFIG_SCHEMA_VERSION / CONFIG_MIGRATIONS
```

### 7.3 测试

```bash
# 量化侧（仓库根目录）
backend/.venv/Scripts/python.exe -m pytest \
  tests/test_quant_core.py tests/test_quant_users.py tests/test_radar_confidence.py \
  tests/test_selection_agents.py tests/test_quant_evolution.py \
  tests/test_quant_optimizations.py -q      # 156 passed
python -m pytest tests/ -q                  # 617 passed / 14 skipped

# 后端侧（backend 目录）
.venv/Scripts/python.exe -m pytest tests/ -q    # 55 passed
```

`tests/test_quant_optimizations.py` 与 `backend/tests/test_analysis_task_state.py`
为本次新增：
- 前者覆盖 P0/P1/A/T 各条目的行为约定（配置迁移幂等、多源回退、仓位双约束、
  行业集中度、择时门、ST 过滤、事件去重、归档清理、观察期分层）；
- 后者覆盖 A5 的跨 worker 契约（远端读状态/读结果、控制指令下发与消费、
  心跳新鲜的不被误杀、旧快照不覆盖行状态）。

> 注意：跑全量时**不要复用** `--basetemp` 目录，pytest 清理旧目录会与
> 沙箱的批量删除确认撞上，表现为 `assert not self._finalizers` 一类假错误。
> 需要固定目录时先手动删掉。

### 7.4 A5 的边界（多 worker 到底能做到什么）

| 能力 | 多 worker 下的表现 |
|---|---|
| 查询进度 `/analysis/status/{id}` | ✅ 任意 worker 都能答（`state_source=snapshot`，滞后 ≤ 1 个心跳周期 5s） |
| 取结果 `/analysis/result/{id}` | ✅ 结果在完成时落 `result_json`，任意 worker 可返回 |
| 暂停 / 恢复 / 停止 | ✅ 指令写 `control` 列，持有执行体的 worker 在 5s 内消费并清空 |
| 任务执行体本身 | ⚠️ 仍在**发起任务的那个 worker** 的线程里；该进程被杀则本次分析中断，靠 checkpoint 重新发起续跑 |
| 量化子系统（消费者 / APScheduler / 清扫器） | ✅ `quant_leader.lock` 文件锁选举：只有 1 个 worker 启动交易子系统，其余只服务 API；领导者退出后内核释放锁，新 worker 接手 |
| 孤儿回收 | ✅ 心跳 >120s 未更新才标 `stopped`（心跳新鲜的不动，多 worker 不互杀） |
| 表结构升级 | ✅ `init_db()` 幂等补列（老库直接启动即可），同时保留 alembic 版本 |

因此 `--workers N` 现在**可以开**：扩容解决的是"接口与轮询的并发承载"，
而不是"把单次分析拆到多进程"。交易子系统靠文件锁只让一个 worker 拥有
（见 `ai_stock/quant/leader.py`）—— 不这么做的话 N 份 APScheduler 会并发发任务。
真要跨进程分发分析任务，下一步是把
`run_analysis_in_thread` 换成 MQ 消费者（`quant_task` 表那套模式可以复用），
属于再往后的迭代。

### 7.5 尚未落地（需要远端运维或后续迭代）

- **部署侧动作**：以上代码变更需同步到 `118.25.48.120` 并重启服务后生效；
  `global_trade_enable` 由配置迁移自动置 1，无需手工改库；systemd 单元需确认
  `Restart=always`（见 `docs/DEPLOYMENT.md` §生产部署）。
- **分析任务跨进程分发**：见 7.4 最后一段，需引入 MQ 消费者，属独立迭代。
- **参数观察**：`RISK_BUDGET_PER_TRADE=1%`、`PORTFOLIO_DD_COEF_THRESHOLD=6%`、`MARKET_MA_WINDOW=20`、`CONF_KICK_THRESHOLD=0.25` 均为经验初值，建议按日报中的胜率/盈亏比/超额收益做 2~3 个月滚动校准。

### 7.6 上线记录（2026-10-17 21:20 / 21:34，`118.25.48.120`）

**动作**：`33ae2a7`（方案落地 + A5）→ `cf9b24c`（停机与日志修复）两次 `git pull` + `systemctl restart`，重启前用 sqlite backup API 备份数据库（`backend/data/aistock.db.bak-20261017_212027`，50MB）。

**上线现场验证**

| 检查项 | 结果 |
|---|---|
| `config migration applied: global_trade_enable 0 -> 1` | ✅ 日志可见（存量库自动迁移，无需手工改库） |
| `max_order_value 200000 -> 250000` / `config_schema_version=3` / `market_timing_enable=1` | ✅ 库里核对一致 |
| `QuantService started: mq=redis, consumers=5, scheduler=apscheduler, global_trade_enable=1` | ✅ |
| `Acquired quant leader lock ... (pid=2237076)`，锁文件 pid == MainPID | ✅ 领导者选举生效 |
| APScheduler 注册 `trigger_slo_check` 等 11 个 job | ✅ 日志可见 |
| `analysis_tasks` 新增 6 列、`quant_stock_pool_holding.industry` | ✅ 幂等补列已跑 |
| `/api/health` | ✅ ok |
| 优雅停机 | ✅ 5 个 consumer 依次 `stopped` → `QuantService stopped`，无异常 |

**上线过程中发现并修复的两个问题**（即 `cf9b24c`）

1. **停机失联**：`TaskConsumer` / `DelayedWorker` / `TimeoutSweeper` / `_FallbackScheduler`
   都把停止标志命名为 `self._stop`，覆盖了 `threading.Thread._stop()` 方法。`join()`
   收尾调用它时抛 `'Event' object is not callable` → `QuantService.stop()` 中断，
   消费者与清扫器没停干净（Redis 阻塞连接与锁残留）。已改名 `_stop_event` 并加
   源码级回归测试。
2. **应用 INFO 日志被吞**：uvicorn 只配置自己的 logger，应用 logger 冒泡到 root
   （默认 WARNING），「QuantService started」这类关键启动日志看不到，排障时极易
   误判成"调度器没起来"。已加 `_configure_logging()`（`LOG_LEVEL` 可覆盖，默认 INFO）。

**首个真实交易日观察清单（下周一 10/19）**

1. `quant_agent_decision_log` 中 `tech_signal` / `tech_no_signal` 比例（预期通过率 13% → 25%+）
2. `industry_scan` 是否还有 `scan_empty`，日志中是否出现 `served by sina_board`
3. 走到 `position_open` 的 flow 是否 > 0，`quant_trade_log` 是否出现新行
4. 若出现 `大盘择时门` 拒绝，确认沪深300 数据是否正常（避免 `unknown` 误放行/误拦截）
5. `check_business_slo` 首次巡检（每 30 分钟）是否误报


### 7.7 复核补记（2026-10-17 22:10，上线后逐条核对）

逐条核对落地情况时又发现并修掉两处，均已上线（`6fad352`、`9dc1fb0`）：

**① `market_timing_enable` 是"哑配置"**
`_market_timing_check()` 只读常量 `MARKET_TIMING_ENABLED`，配置表里的同名键形同摆设 ——
运营在界面拨开关不生效。已改为优先读库（缺省回落常量），与 `global_trade_enable`
同一套"无需重启"语义，并补用例（开关=1 拦截 / =0 放行）。

**② 东财个股行情接口同样被封（比板块更危险）**
干跑发现 `/api/qt/stock/get` 与 `clist` 一起 `RemoteDisconnected`，实时价只能退过期缓存
→ 涨停/跌停判断、单笔金额校验、ATR 定量全部失真。已补「东财 push2 → 腾讯 qt.gtimg.cn」
双源 + 失败计入数据源健康矩阵（3 次 → 5/15/60min 退避，避免一轮扫描里每只票白等 20s）。
涨跌停价优先用数据源口径（腾讯含 ST 5%/北交所 30% 等特殊比例），缺失才按昨收推算。

**生产实测（22:00，`118.25.48.120`）**

| 数据链路 | 结果 |
|---|---|
| 行业板块列表 | 49 条，`source=sina_board`（东财连续失败 2 次后自动切） |
| 概念板块列表 | 175 条，`source=sina_board` |
| `get_all_industries()` | 224 条（行业+概念合集） |
| 板块成分股 | 正常：船舶制造 → 600150 中国船舶 35.77 / 600685 中船防务 28.13 |
| 实时行情 | `source=tencent`，688002 睿创微纳 164.77（涨跌停 201.02/134.02，科创板 20% 口径正确） |
| 沪深300（择时门/基准） | 4170.61，MA20 4385.89 |

**当前唯一的"不下单"原因：大盘择时门判定 risk_off**

`沪深300 4170.61 < MA20 4385.89`（低 4.9%），按 T3 设计"只减不加" → 周一买入会被
`pre_trade_check` 全部拒绝。这是策略选择而非故障，三个选项：

- **保持开启（默认，方案原意）**：等指数站上 MA20 且均线上翘再建仓，回撤风险最低
- **临时关闭**：`UPDATE quant_system_config SET value='0' WHERE key='market_timing_enable'`（即时生效，无需重启）
- **放宽判定**：如"收盘 > MA20×0.97 即可"，需要改 `market_regime.get_market_regime` 加容差参数

> 注意：即使关掉择时门，趋势动量通道要求"站上 MA20 + MA20 上翘 + 放量"，弱市里命中率
> 依然低；均值回归三通道（BOLL 缩量止跌 / MACD 底背离 / 缺口回踩）才是弱市主力。

**决策记录（2026-10-17 22:30）**：按"先验证链路"原则，已在生产将
`market_timing_enable` 置 **0**（即时生效，无需重启），干跑 3 只池内标的
`pre_trade_check` 全部 `passed`。择时判定仍可观测（`get_market_regime()` 照常返回
`risk_off`，日报基准不受影响），只是不再拦截建仓。

恢复开关（任选其一，均即时生效）：

```sql
UPDATE quant_system_config SET value='1' WHERE key='market_timing_enable';
```

```bash
# 或走 API（需登录 token）
curl -X PUT http://127.0.0.1:8000/api/quant/config \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"key":"market_timing_enable","value":"1"}'
```

建议在拿到 3~5 笔完整成交、确认「选股 → 开仓 → 持仓跟踪 → 止损/止盈 → 日报归因」
全链路无异常后再打开。

**补充决策（2026-10-17 22:41）**：`max_drawdown_pct` 保持库里的 **0.10（10%）**，
不与代码种子 `0.20` 对齐 —— 这是人工保留的更严口径，不是配置漂移。
首轮验证阶段 10% 触顶即冻结，风险更可控；后续若需放宽，改库即可（即时生效）。

**补充决策（2026-10-17 22:41）**：`max_drawdown_pct` 保持库里的 **0.10（10%）**，
不与代码种子 `0.20` 对齐 —— 这是人工保留的更严口径，不是配置漂移。
首轮验证阶段 10% 触顶即冻结，风险更可控；后续若需放宽，改库即可（即时生效）。
