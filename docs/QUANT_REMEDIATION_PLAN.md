# 量化链路修复实施计划

> 版本：v1.0 ｜ 编制日期：2026-10-21 ｜ 对应需求：`docs/QUANT_REMEDIATION_SPEC.md`
> 代码基线：当前工作区 HEAD（`ai_stock/quant/`）
> 本文档是**施工图**：每个改动落在哪个文件的哪个函数、改成什么、怎么验、怎么回滚。
> 所有决策已在此锁定，不再保留"待定"分支。

---

## 0. 计划基线

### 0.1 运行环境（已核实）

| 用途 | 命令/路径 |
|---|---|
| 测试 | `cd backend && .venv/bin/python -m pytest tests/ -q`（根目录 `.venv` 无 pytest） |
| 生产 | `118.25.48.120:/deploy/AI_stock/backend`，systemd `ai-stock-backend` |
| 数据库 | sqlite `backend/data/aistock.db`，量化表由 `QuantBase.metadata.create_all` 建（`ai_stock/quant/db.py:139`） |
| 迁移 | 只有 `backend/migrations`（Alembic）管后端表；**量化表没有迁移链路** |

### 0.2 硬约束

1. **不改**生产 `max_drawdown_pct = 0.10`（代码种子 0.20 是历史遗留，不得"对齐"）。
2. **不动** `trading_session_guard_enable`：买卖同闸是正确设计，禁止为"盘后补止损"开洞。
3. **不引入 DDL**：量化表无 Alembic 链路，`create_all` 不会给已存在的表补列。本计划所有新增状态
   一律落在 **JSON 字段**或 **`quant_system_config` 键值**，不新增列、不新增表。
4. **不依赖配置种子**：`service._seed_system_config()` 只插入不覆盖（10-17 复盘已确证），
   新配置一律"读取时带代码默认值"，种子只用于新装环境。
5. 每批改动必须自带回滚开关，且开关关闭后行为与改动前**逐字节一致**。

---

## 1. 锁定的技术决策

需求书里有 4 处需要拍板，这里直接定下来并说明为什么，后续施工不再讨论。

### D1 回撤基准：持久化历史净值高点（HWM）

**存两把锁**：`quant_system_config` 新增 `account_hwm`（高点）与 `account_hwm_initial`（记账时的初始资金）。
读取时若 `account_hwm_initial != 当前 initial_cash`，判定为账户已重置 → 用 `max(initial_cash, 当前总资产)` 重新起算。

**为什么不用 `max(initial, current)`**：它在盈利态恒等于 `current`，回撤永远为 0（需求书 FR-01）。
**为什么不用"每日落库快照表"**：需要新表 + 回填历史，收益不抵成本；HWM 是单调量，一个键足够。
**为什么带 `account_hwm_initial`**：全仓检索确认没有任何代码写 `initial_cash`（`config.py:386` 是种子，其余全是读），
重置靠人工改库。用"初始资金变化"作为重置信号，是唯一不依赖人工记忆的自动化判据。

### D2 `pending_exit` 落点：`plan_json`

`quant_stock_pool_holding` 已有 `plan_json`，且已在承载执行态字段（`stop_loss_price`、`sizing`、`history`，
见 `buy.py:1311-1322`、`hold.py:626-632`）。把 `pending_exit` 放进去，零 DDL、随持仓生命周期自动清理。

**读写必须原子**：新增 `db_ops.mutate_holding_plan(symbol, mutator)`，在**同一个 session** 内读-改-写，
避免 `_update_plan` 与 `pending_exit` 写入互相覆盖。

### D3 `risk_reward` 缺失：重试一次 → 按策略处置（默认拒绝）

**先重试**：第二次调用在提示词末尾追加"risk_reward 必须输出纯数字（收益/风险），不得留空"。
**仍缺失**：读运行时键 `rr_missing_policy`，`reject`（默认，fail-closed）或 `warn`（只记 `reject_stage="rr_missing"` 放行）。

**为什么默认 reject**：买入链路其余关卡都是 fail-closed（LLM 不可用直接拒绝，`buy.py:1071-1078`），
唯独这里 fail-open，是口径不一致而不是容错。`warn` 只用于灰度首周观察缺失率，**不是**长期状态。

### D4 止损价格源：确认"盘中触发"，并修正回测口径

**选盘中**，理由不是偏好而是系统约束：非交易时段一律拒单（`risk_control.py:103-110`），
"收盘确认"意味着只能次日开盘卖，隔夜跳空风险大于盘中假破位风险——这与 `hold.py:159-161`
记载的"原实现要 2 根确认、急跌亏 8~12%"是同一个判断。

**具体动作**：① 删除"盘中破位只留痕"的误导注释，改为"止损按实时价判定，破位即卖"；
② `evolution.backtest_stop_policy` 增加可选 `lows` 参数，传了就按当日最低价判破位，
让回测与实盘同口径（现有测试只传 `closes`，加默认 `None` 向后兼容）。

### D5 重大利空清仓：两段式确认 + 跌幅旁路

第一次判定 → 写 `pending_exit(reason="major_bear", confirm_needed=1)`，当日不卖；
下一轮再次判定为真，或当日跌幅 ≥ 3%，才清仓。硬止损信号优先，不受本机制约束。

**为什么加 3% 旁路**：真利空的第一天往往就是大跌，纯两段式会白亏一天；3% 与
`max_daily_loss_pct` 同量级，不是新拍的数字。

### D6 止盈：触及止盈价减半仓；移动止损三档

三档表（浮盈 → 止损位上移至成本 × 系数）：`6% → 1.00`、`12% → 1.06`、`20% → 1.12`。
止盈触及卖 1/2，余仓交给移动止损。

**为什么不清仓**：现有 `sell_partial` 通道已存在（`hold.py:370-372`，portion=0.5），复用零成本；
清仓会把"让利润奔跑"直接掐断。

### D7 跌停卖单：改为挂单（PENDING），并放开 `pre_trade_check` 的拦截

**必须两处一起改**，否则 broker 的改动不可达：`risk_control.py:165-167` 会先拒单，
永远走不到 `broker.py:345-346`。

**不写 pending 状态的 `trade_log`**：`count_orders_today` 统计当日**全部**状态行（含 rejected，
`db_ops.py:520-537`），挂单若落库，同一笔委托会占用两次笔数限额。挂单状态只存 `pending_exit`
与决策日志，成交时才写 trade_log。

**副作用是正向的**：放开后，跌停日的反复重试不再刷 `trade_log`，不会挤占 `max_daily_orders=50`。

### D8 择时门：收紧要快、放松要慢

`risk_on` 需**连续 2 个交易日**满足且**收盘高于 MA20 至少 1%**；`risk_off` 立即生效。
`unknown` 连续 24h 告警，连续 3 个交易日 → 仓位系数 × 0.5（只收紧不阻断）。

**为什么带 1% 缓冲**：纯"连续 2 日"在均线附近仍会翻转，缓冲把"贴着均线震荡"直接排除在 `risk_on` 之外。

---

## 2. 文件变更总表

| 文件 | 类型 | 涉及需求 | 改动概要 |
|---|---|---|---|
| `ai_stock/quant/risk_control.py` | 改 | FR-01/02/03/08/13 | `account_snapshot` 重写、HWM、MTM 盈亏、卖单跌停放行、择时门调用参数 |
| `ai_stock/quant/db_ops.py` | 改 | FR-02/10/14 | 新增 `mutate_holding_plan`、`get_decisions(since=)` |
| `ai_stock/quant/config.py` | 改 | 全部 | 新增常量块（见 §4），不改任何现有值 |
| `ai_stock/quant/agents/hold.py` | 改 | FR-02/05/06/07/13 | 卖单状态机、多档移动止损、止盈减半、利空两段式、注释修正 |
| `ai_stock/quant/agents/buy.py` | 改 | FR-04/05/09 | `risk_reward` 重试与策略、`extra={"used_ratio"}`、档位初值 |
| `ai_stock/quant/agents/risk.py` | 改 | FR-02 | 强制减仓失败写 `pending_exit` |
| `ai_stock/quant/broker.py` | 改 | FR-09/13 | 跌停卖单 PENDING、实盘适配器补风控、装配自检 |
| `ai_stock/quant/market_regime.py` | 改 | FR-08 | 连续确认 + 缓冲 + unknown 计数持久化 |
| `ai_stock/quant/evolution.py` | 改 | FR-06 | `backtest_stop_policy(..., lows=None)` |
| `ai_stock/pipeline/news_impact.py` | 改 | FR-11 | `_freshness` 统一东八区 |
| `ai_stock/quant/theme_registry.py` | 改 | FR-12 | 覆盖比例门槛 |
| `scripts/trade_attribution.py` | 新增 | FR-14 | 只读归因报表 |
| `ai_stock/quant/exit_state.py` | 新增 | FR-02/07 | `pending_exit` 读写封装（单一入口） |
| 8 个测试文件 | 新增 | 全部 | 见 §5 |

---

## 3. 施工批次

### Batch 1：账户度量纠正（FR-01 + FR-03）

**为什么排第一**：Batch 2/3 会改变卖出与建仓行为，若账户度量仍是错的，观察期看到的所有数字
（冻结次数、仓位系数、回撤）都无法归因。先修尺子，再动动作。

#### 1.1 `risk_control.py::account_snapshot` 重写

```python
HWM_KEY = "account_hwm"
HWM_INITIAL_KEY = "account_hwm_initial"

def account_snapshot() -> dict:
    broker = get_broker()
    try:
        info = broker.get_account_info()
    except Exception as exc:
        logger.warning("account snapshot failed: %s", exc)
        # 保持既有语义: 取数失败一律回零, 且**不触碰 HWM**(不能把 0 写进高点)
        return {"available_cash": 0.0, "market_value": 0.0, "total_assets": 0.0,
                "drawdown_pct": 0.0, "daily_pnl": 0.0, "daily_pnl_total": 0.0,
                "drawdown_basis": "unavailable", "mtm_incomplete": []}

    initial = float(db_ops.get_config_value("initial_cash", "1000000"))
    hwm = _resolve_hwm(initial, info.total_assets)
    drawdown = (hwm - info.total_assets) / hwm if hwm > 0 else 0.0

    realized = db_ops.get_realized_pnl_today()
    mtm, incomplete = _intraday_mtm_pnl(broker)
    return {
        "available_cash": info.available_cash,
        "market_value": info.total_market_value,
        "total_assets": info.total_assets,
        "drawdown_pct": round(drawdown, 4),
        "daily_pnl": round(realized, 2),                  # 保留: 前端/报表在用
        "daily_pnl_total": round(realized + mtm, 2),      # 新增: 风控判据用
        "drawdown_basis": "hwm",
        "mtm_incomplete": incomplete,
    }
```

```python
def _resolve_hwm(initial: float, total_assets: float) -> float:
    """历史净值高点; 初始资金变化视作账户重置, 自动重设基准."""
    stored = db_ops.get_config_value(HWM_KEY, "").strip()
    basis = db_ops.get_config_value(HWM_INITIAL_KEY, "").strip()
    try:
        hwm = float(stored) if stored else 0.0
        base = float(basis) if basis else None
    except ValueError:
        hwm, base = 0.0, None
    if base is None or abs(base - initial) > 0.01 or hwm <= 0:
        hwm = max(initial, total_assets, 0.0)      # 首次上线/重置: 不制造伪回撤
        db_ops.set_config_value(HWM_INITIAL_KEY, f"{initial:.2f}", "HWM 记账时的初始资金")
        db_ops.set_config_value(HWM_KEY, f"{hwm:.2f}", "账户历史净值高点(回撤基准)")
        return hwm
    if total_assets > hwm:
        hwm = total_assets
        db_ops.set_config_value(HWM_KEY, f"{hwm:.2f}", "账户历史净值高点(回撤基准)")
    return hwm
```

```python
def _intraday_mtm_pnl(broker) -> tuple[float, list[str]]:
    """持仓浮盈浮亏(相对今日基准). 基准: 当日买入用成本, 隔日持仓用昨收."""
    # 日界口径必须与 broker 的 buy_date 一致: 都是北京时间日历日 %Y-%m-%d
    # (broker.py:195-197、:41-49)。用 _now_market 而非 get_trade_date:
    # 后者在非交易日会回溯, 且 conftest 的 _pin_trading_clock 钉的就是它。
    today = _now_market().strftime("%Y-%m-%d")
    positions = broker.get_positions()
    if not positions:
        return 0.0, []
    quotes = get_data_service().get_batch_quotes([p.symbol for p in positions])
    total, incomplete = 0.0, []
    for p in positions:
        q = quotes.get(p.symbol) or {}
        price = float(q.get("price") or p.last_price or 0.0)
        # 关键: MTM 用 total_quantity(含 T+1 冻结), 不是 quantity(可用量)
        base = float(p.avg_cost or 0.0) if p.buy_date == today else float(q.get("prev_close") or 0.0)
        if price <= 0 or base <= 0:
            incomplete.append(p.symbol)
            continue
        total += int(p.total_quantity or 0) * (price - base)
    return total, incomplete
```

`get_batch_quotes` 走 `get_realtime_quote` 缓存（`data_service.py:629-658`，含熔断与过期兜底），
`account_snapshot` 每笔下单调用一次的开销可控；持仓上限 3 只，最坏 3 次缓存查询。

#### 1.2 冻结判据切换

`risk_control.py:120-124` 与 `:381-385` 两处，把 `snap.get("daily_pnl")` 换成
`snap.get("daily_pnl_total", snap.get("daily_pnl", 0.0))`（带 fallback，兼容外部传入的旧结构 snapshot）。

#### 1.3 验收

| 用例 | 断言 |
|---|---|
| HWM 首次初始化（无配置键，资产 100 万） | `account_hwm=1000000`，`drawdown_pct=0` |
| 涨到 150 万 → 回落到 120 万 | `drawdown_pct=0.20`，`pre_trade_check(BUY)` 拒绝且 `trade_frozen=1` |
| 涨到 150 万 → 146 万 | `drawdown_pct≈0.0267`，买入通过；`buy.py` 仓位系数不折半 |
| `initial_cash` 从 100 万改为 50 万（重置） | HWM 立即 rebase 为 `max(50万, 当前资产)`，不产生虚假回撤 |
| 持仓 60 万当日跌 6%，无卖出 | `daily_pnl_total≈-3.6 万`，超 3 万阈值触发冻结 |
| 行情接口抛异常 | `drawdown_pct=0`、`drawdown_basis="unavailable"`，HWM 不被写坏 |

#### 1.4 回滚

`drawdown_basis` 读取时若运行时键 `drawdown_basis=initial`，则 `_resolve_hwm` 直接返回
`max(initial, total_assets)`（旧口径），无需改代码。

---

### Batch 2：卖出可靠性（FR-02 + FR-13 + FR-06 注释/回测口径）

**依赖**：Batch 1 完成（否则回撤/盈亏数字不可信，无法判断卖单行为变化是否合理）。

#### 2.1 新增 `ai_stock/quant/exit_state.py`（单一入口，禁止各处直接摸 plan）

```python
PENDING_KEY = "pending_exit"

def get_pending_exit(holding: dict) -> dict | None:
    return (holding.get("plan") or {}).get(PENDING_KEY) or None

def mark_pending_exit(symbol, *, reason, portion=1.0, error="", confirm_needed=0) -> dict:
    """写入/累加退出意图; 返回最新状态. 幂等: 已存在则只累加 fail_count."""
    def _mut(plan):
        cur = plan.get(PENDING_KEY) or {}
        plan[PENDING_KEY] = {
            "reason": reason or cur.get("reason", ""),
            "portion": float(cur.get("portion", portion)),
            "first_at": cur.get("first_at") or _now_iso(),
            "last_attempt_at": _now_iso(),
            "fail_count": int(cur.get("fail_count", 0)) + (1 if error else 0),
            "last_error": error,
            "confirm_needed": int(confirm_needed),
        }
    db_ops.mutate_holding_plan(symbol, _mut)
    return (db_ops.get_holding(symbol) or {}).get("plan", {}).get(PENDING_KEY)

def clear_pending_exit(symbol) -> None:
    db_ops.mutate_holding_plan(symbol, lambda plan: plan.pop(PENDING_KEY, None))

def should_retry(pending: dict) -> bool:
    """冷却 + 日限流: 30 分钟一次, 每标的每日最多 6 次."""
```

`db_ops.mutate_holding_plan`：

```python
def mutate_holding_plan(symbol: str, mutator, user_id: str | None = None) -> dict | None:
    """原子读-改-写 plan_json (单 session), 避免并发覆盖."""
    from .config import SYSTEM_USER
    uid = user_id or SYSTEM_USER
    with session_scope() as s:
        row = s.get(StockPoolHolding, (uid, symbol))
        if row is None:
            return None
        plan = json.loads(row.plan_json or "{}")
        mutator(plan)
        row.plan_json = json.dumps(plan, ensure_ascii=False)
        s.commit()
        return plan
```

#### 2.2 卖出执行状态机（`hold.py::_execute_sell`）

改动后逻辑（伪码 + 关键语句）：

```python
check = pre_trade_check(order)
if not check.ok:
    pending = exit_state.mark_pending_exit(
        symbol, reason=reduce_result.get("reason", ""), portion=portion, error=check.reason,
    )
    level = ERROR if pending["fail_count"] >= 3 else WARNING
    get_alerts().emit(level, f"sell_blocked:{symbol}",
                      f"{symbol} 卖出被拒({check.reason}), 累计 {pending['fail_count']} 次")
    self.log("sell_risk_rejected", ...)
    return {"op": "sell", "status": "risk_rejected", "pending_exit": pending}

result_order = broker.place_order(order)
if result_order.status == OrderStatus.PENDING:          # 跌停挂单
    exit_state.mark_pending_exit(symbol, reason="跌停挂单排队", portion=portion)
    return {"op": "sell", "status": "pending", "message": result_order.message}
if result_order.status.value == "filled":
    exit_state.clear_pending_exit(symbol)               # 成交必清意图
    ...既有持仓更新与 fanout...
```

#### 2.3 扫描入口优先处理退出意图（`hold.py::handle_hold_scan`）

```python
for holding in db_ops.get_holdings():
    pending = exit_state.get_pending_exit(holding)
    if pending and not exit_state.should_retry(pending):
        continue                                  # 冷却中, 不启动 flow
    orchestrator.start_flow("hold", flow_id=..., data={..., "force_exit": pending})
```

`ReducePositionAgent` 顶部：

```python
force = context.get("force_exit")
if force:
    result = {"decision": "sell_all", "action": "sell_all", "portion": float(force.get("portion", 1.0)),
              "reason": f"退出意图重试: {force.get('reason','')}"}
    ...log("reduce_sell_all") / report / return
```

这样跳过 LLM 与趋势判断——价格已破位，不该因为当日反弹撤销退出。

#### 2.4 跌停放行（`risk_control.py:165-167` + `broker.py:344-346`）

`risk_control.py` 删除跌停拒卖分支（保留涨停拒买）；`broker.py` 改为：

```python
if request.side == OrderSide.SELL and price <= limit_down > 0:
    return OrderResult(order_id=order_id, status=OrderStatus.PENDING,
                       message=f"跌停价 {limit_down}, 卖单挂单排队")
```

**注意**：此处**不**写 `trade_log`（D7），`_ensure_state` 只回放 `filled`（`broker.py:235-236`），
即使后续误写也不影响状态重建。

#### 2.5 强制减仓路径同步（`risk.py`）

`risk.py:80-86` 的 `risk_rejected` 分支补一句 `exit_state.mark_pending_exit(...)`，与 hold 侧共用状态。

#### 2.6 FR-06 口径落地

- `hold.py:176-181`：注释改为"止损按实时价判定；`intraday_breach` 保留为观测字段（当日最高价破位但收盘未破）"。
  实现上补 `intraday_breach` 语义：`high_series[-1] <= dynamic_stop < current_close`，避免字段永远为 False。
- `evolution.backtest_stop_policy(closes, ..., lows=None)`：`lows` 传入时用 `low <= stop` 判破位，返回增加
  `exit_price_source: "low"|"close"`。`_collect_series` 同步收集 `Low` 列，`_segment_results` 透传。

#### 2.7 验收

1. 跌停持仓连续 3 轮扫描：不写 trade_log、`pending_exit.fail_count=3`、第 3 轮出现
   `sell_blocked:<symbol>` ERROR 告警、`count_orders_today()` 不变。
2. 跌停打开后第一轮即成交，`pending_exit` 被清除，trade_log 新增 1 条 filled。
3. 行情不可得：卖单不再返回"拒绝下单"，而是进入 `pending_exit`。
4. 退出意图存续期间，`reduce_position` 不再调用 LLM（用 mock 断言调用次数 0）。
5. `backtest_stop_policy` 传 `lows` 时，破位日在收盘价未破、最低价破位的场景下正确触发 `stop_loss`。

#### 2.8 回滚

运行时键 `sell_retry_enabled=0` → `handle_hold_scan` 不读 `pending_exit`、`_execute_sell` 不写状态，
行为回到"只记日志"。跌停放行可用 `allow_limit_down_sell=0` 关回拒单。

---

### Batch 3：决策口径（FR-05 + FR-07 + FR-04）

**依赖**：Batch 2（FR-07 复用 `pending_exit` 状态机）。

#### 3.1 多档移动止损（`hold.py::AddPositionAgent`）

`config.py` 新增：

```python
TRAILING_STOP_TIERS = ((0.06, 0.00), (0.12, 0.06), (0.20, 0.12))  # (浮盈阈值, 成本上浮比例)
```

```python
new_stop_loss = None
for trigger, floor in TRAILING_STOP_TIERS:
    if gain_ratio >= trigger:
        new_stop_loss = round(cost * (1 + floor), 3)
if new_stop_loss and new_stop_loss <= float(context.get("stop_loss") or 0):
    new_stop_loss = None            # 只上移, 系统级保证
```

原 `TRAILING_STOP_TRIGGER/FLOOR` 保留（其他引用点不动），但此路不再读取。

#### 3.2 止盈减半（`hold.py::ReducePositionAgent`）

```python
take_profit = float(context.get("take_profit") or 0)
price = float(trend_result.get("current_close") or 0)
if action == "hold" and take_profit > 0 and price >= take_profit:
    action, portion = "sell_partial", 0.5
    reason = f"触及止盈价 {take_profit}, 减仓 1/2 交由移动止损管理余仓"
```

位置放在硬止损/时间止损**之后**（止损优先），利空判断**之前**（止盈不该被利空拦）。

#### 3.3 利空两段式（`hold.py::ReducePositionAgent`）

```python
major_bear = self._check_major_bear(symbol, context)
if major_bear and major_bear.has_major_bear:
    pending = exit_state.get_pending_exit(db_ops.get_holding(symbol) or {})
    drop = float(trend_result.get("loss_ratio", 0))
    if pending and pending.get("confirm_needed"):
        action, reason = "sell_all", f"利空二次确认: {major_bear.reason}"
    elif drop >= MAJOR_BEAR_BYPASS_DROP:          # 0.03
        action, reason = "sell_all", f"利空且当日跌幅 {drop:.1%}, 直接清仓"
    else:
        exit_state.mark_pending_exit(symbol, reason=f"重大利空待确认: {major_bear.reason}",
                                     portion=1.0, confirm_needed=1)
        action, reason = "hold", f"利空首次判定, 待下轮确认: {major_bear.reason}"
```

LLM 不可用的现有行为（返回 `has_major_bear=False`，不卖）保持不变。

#### 3.4 收益比缺失的重试与策略（`buy.py::SecondVerificationAgent`）

`_parse_risk_reward` 保持"解析不出来返回 0.0"的签名不变（其他调用点不受影响），
新增一个判定层，把"0"拆成"缺失"与"明确为 0"两种语义：

```python
RR_RETRY_SUFFIX = (
    "\n\n上一次输出缺少数值型 risk_reward。请重新输出完整 JSON，"
    "其中 risk_reward 必须是纯数字（预期收益 ÷ 止损风险），不得留空或写区间。"
)

def _rr_state(report: VerificationReport) -> tuple[float, str]:
    """返回 (收益风险比, 状态). 状态: ok / missing / invalid."""
    if report.risk_reward and float(report.risk_reward) > 0:
        return float(report.risk_reward), "ok"
    rr = _parse_risk_reward(report)            # 文本回退, buy.py:129
    if rr > 0:
        return rr, "ok"
    return 0.0, ("missing" if not (report.risk_reward_ratio or "").strip() else "invalid")
```

调用处（`buy.py:1067-1070` 之后）：

```python
rr, rr_state = _rr_state(report)
if rr_state != "ok":
    try:                                       # 只重试一次, 复用同一 deep 模型
        report = structured_invoke(
            self.llm.pick(deep=True), VerificationReport, prompt + RR_RETRY_SUFFIX, deep=True,
        )
        rr, rr_state = _rr_state(report)
    except Exception as exc:
        logger.warning("rr retry failed for %s: %s", symbol, exc)
```

判定（替换 `buy.py:1100-1104`）：

```python
policy = db_ops.get_config_value("rr_missing_policy", "reject").strip().lower()
weak_rr = bool(MIN_RISK_REWARD_RATIO > 0 and 0 < rr < MIN_RISK_REWARD_RATIO)
rr_blocked = bool(rr_state != "ok" and policy != "warn")
passed = bool(report.passed) and not bear_blocked and not weak_rr and not rr_blocked
```

`reject_stage` 优先级补一档：`llm_rejected → bear_* → weak_risk_reward → rr_missing`
（`rr_blocked` 成立时取 `rr_missing`）。`result` 中新增 `"rr_state": rr_state`，
`detail` 保留 `risk_reward_ratio` 原文（现有 `buy.py:1127` 已在写）。

**为什么重试而不是直接拒**：`structured_invoke` 在 `with_structured_output` 失败时会降级到
自由文本 + JSON 提取（`llm_helper.py:86-99`），字段缺失大多来自降级路径，重试一次的成功率高于
直接放弃；两次都缺才判死。

#### 3.5 验收

1. 浮盈 7% → `new_stop_loss == cost`；13% → `cost*1.06`；回落至 4% 时 `new_stop_loss is None`。
2. 触及止盈价且无止损信号 → `sell_partial`，成交后持仓数量减半，`stop_loss` 不变。
3. 单次利空判定 → 当日 `action == "hold"` 且 `pending_exit.confirm_needed == 1`；
   下一轮再判定为真 → `sell_all`。
4. 利空 + 当日跌幅 3.2% → 首轮即 `sell_all`。
5. 硬止损与利空同时成立 → `sell_all`（走硬止损分支，reason 含"硬性止损"）。
6. LLM 首次返回 `risk_reward=0` 且文本为空 → 触发第二次调用（mock 断言调用次数 2）；
   第二次仍缺失且 `rr_missing_policy=reject` → 拒绝，`reject_stage="rr_missing"`；
   `policy=warn` → 放行且 `detail.rr_state=="missing"`。
7. `risk_reward_ratio="1:1.2"` → 解析 1.2 → `weak_risk_reward` 拒绝；
   `"3:1"` → 解析 3.0 → 通过。

#### 3.6 回滚

`trailing_tiers_enabled=0` → 走原单档；`take_profit_partial_enabled=0` → 止盈不动作；
`major_bear_confirm_rounds=0` → 单次判定即清仓（原行为）；
`rr_missing_policy=warn` → 收益比缺失不拦截（等价改动前）。

---

### Batch 4：择时与实盘（FR-08 + FR-09）

#### 4.1 择时门（`market_regime.py`）

新增状态（进程内 + 配置持久化，避免重启丢计数）：

```python
CONFIRM_STATE_KEY = "market_regime_state"   # JSON: {risk_on_streak, unknown_since, unknown_days}
CANDIDATE_BUFFER = 0.01                     # 收盘需高于 MA20 至少 1%
```

判定改为：

```python
above_strong = close > ma_now * (1 + CANDIDATE_BUFFER)
if above_strong and rising:
    streak += 1
    if streak >= MARKET_CONFIRM_DAYS:      # 2
        regime = "risk_on"
    else:
        regime = "risk_off"                # 未确认前按 risk_off, 只减不加
else:
    streak = 0
    regime = "risk_off"
```

`unknown` 计数与 `unknown_since` 写 `market_regime_state`；`risk_control._market_timing_check` 之外，
`PositionOpenAgent` 在建仓前读同一状态，`unknown_days >= 3` 时 `coef *= 0.5`（与回撤折半同一处系数逻辑）。

#### 4.2 实盘适配器（`broker.py`）

```python
def _ensure_live_permission(self, request: OrderRequest) -> None:
    ...既有 4 项...
    # 延迟导入避免循环依赖 (risk_control 模块顶部 import 了本模块)
    from .risk_control import pre_trade_check
    ratio = (request.extra or {}).get("used_ratio")
    check = pre_trade_check(request, position_ratio=ratio)
    if not check.ok:
        raise OrderRejectException(f"实盘风控拒绝: {check.reason}")
```

**必须同时改两处调用方**，否则 broker 层用默认 20% 复核会把 24% 的高置信建仓单二次拒掉：

- `buy.py:1256-1261`：`OrderRequest(..., extra={"used_ratio": used_ratio})`
- `hold.py:561-566`（加仓）：`extra={"used_ratio": BUY_POSITION_RATIO}`

装配自检：`set_broker()` 内打印 broker 类型并校验 `global_trade_enable`（实盘必须显式 `=1`），
不通过直接抛异常，不允许静默降级。

#### 4.3 验收

1. 指数连续 2 日站上 MA20 且超出 1% → `risk_on`；仅 1 日 → `risk_off`。
2. 指数在 MA20 ±0.5% 震荡 5 日 → `risk_on` 一次都不出现。
3. 数据源连续失败：24h 后出现 `market_regime_unknown` 告警；第 3 个交易日建仓系数 × 0.5。
4. 直接对 HuaxingBrokerAdapter 调 `place_order`（绕过业务层）：`trade_frozen=1` 时被拒；
   `used_ratio=0.24` 的合法单不被二次拒。

#### 4.4 回滚

`market_confirm_days=1` + `market_buffer=0` 即回原逻辑；`live_pre_trade_guard=0` 关闭 broker 层复核。

---

### Batch 5：打磨项（FR-10 / FR-11 / FR-12 / FR-14）

| 需求 | 改动 | 验收 |
|---|---|---|
| FR-10 | `db_ops.get_decisions(since: datetime | None = None)` 加时间过滤；`_cooling_symbols` 传 `since=now-240min`、`limit=2000` | 1000 条早期拒绝 + 1 条 200 分钟前拒绝 → 命中；300 分钟前 → 不命中 |
| FR-11 | `_freshness` 用 `datetime.now(timezone(timedelta(hours=8)))`；抽 `market_now()` 到 `calendar_utils` 供三处复用 | 主机设 UTC 与东八区结果一致；49 小时前 `freshness=0.1` |
| FR-12 | `len(hist) >= max(6, ceil(0.7*len(symbols)))`，不满足 → `shadow` 并记 `metrics.coverage` | 8 只中 5 只有历史 → 不再 active；7 只 → active 且 `coverage=0.875` |
| FR-14 | 新增 `scripts/trade_attribution.py`（只读）；`evolution.evolve_stop_loss` 加 `--dry-run` 入口由脚本调用 | 漏斗各环节数字与 `quant_agent_decision_log` 手工统计一致；脚本不改任何配置 |

**FR-12 前置动作**：先在生产只读跑一遍新阈值，导出会被打回的主题清单；
若打回比例 >50%，说明现有主题普遍样本不足，需连同 `GATE_MIN_HISTORY_DAYS` 一起复议，**不得**为保住主题数而放宽。

---

## 4. 配置项清单

### 4.1 代码常量（`config.py`，需发版）

| 常量 | 值 | 说明 |
|---|---|---|
| `TRAILING_STOP_TIERS` | `((0.06,0.00),(0.12,0.06),(0.20,0.12))` | 多档移动止损 |
| `TAKE_PROFIT_PARTIAL_PORTION` | `0.5` | 止盈减仓比例 |
| `MAJOR_BEAR_BYPASS_DROP` | `0.03` | 利空直接清仓的当日跌幅旁路 |
| `MARKET_CONFIRM_DAYS` | `2` | risk_on 连续确认天数 |
| `MARKET_CANDIDATE_BUFFER` | `0.01` | 站上 MA20 的缓冲 |
| `MARKET_UNKNOWN_WARN_HOURS` | `24` | 数据故障告警阈值 |
| `MARKET_UNKNOWN_HALF_DAYS` | `3` | 数据故障半仓阈值 |
| `SELL_RETRY_COOLDOWN_MINUTES` | `30` | 卖出重试冷却 |
| `SELL_RETRY_MAX_PER_DAY` | `6` | 单标的每日重试上限 |
| `THEME_MIN_HISTORY_COVERAGE` | `0.7` | 主题可用日线覆盖比例 |

### 4.2 运行时键（`quant_system_config`，改库即生效，代码内带默认）

| 键 | 默认 | 作用 | 回滚值 |
|---|---|---|---|
| `account_hwm` | 自动 | 净值高点 | — |
| `account_hwm_initial` | 自动 | 重置检测 | — |
| `drawdown_basis` | `hwm` | 回撤口径 | `initial` |
| `rr_missing_policy` | `reject` | 收益比缺失处置 | `warn` |
| `sell_retry_enabled` | `1` | 退出意图重试 | `0` |
| `allow_limit_down_sell` | `1` | 跌停卖单放行 | `0` |
| `trailing_tiers_enabled` | `1` | 多档止损 | `0` |
| `take_profit_partial_enabled` | `1` | 止盈减仓 | `0` |
| `major_bear_confirm_rounds` | `1` | 利空确认轮次 | `0` |
| `market_confirm_days` | `2` | 择时确认 | `1` |
| `live_pre_trade_guard` | `1` | 实盘 broker 层复核 | `0` |

**注意**：以上键**不要**塞进 `SYSTEM_CONFIG_SEED` 后指望生产生效——种子只插不覆盖。
读取处一律 `db_ops.get_config_value(key, DEFAULT)`，生产按 §6 用 `set_config_value` 逐条写入。

---

## 5. 测试计划

### 5.1 新增测试文件

| 文件 | 覆盖 | 关键夹具 |
|---|---|---|
| `tests/test_risk_account_metrics.py` | FR-01/03：HWM、重置、MTM、冻结 | `quant_env` + `_FakeBroker` |
| `tests/test_sell_exit_state.py` | FR-02/13：状态机、重试、跌停挂单、告警、笔数不涨 | `_FakeBroker`(可切跌停/异常) |
| `tests/test_hold_decisions.py` | FR-05/07：三档止损、止盈减半、利空两段式 | mock `_check_major_bear` |
| `tests/test_market_regime_confirm.py` | FR-08：连续确认、缓冲、unknown 计数 | 注入收盘序列 |
| `tests/test_broker_live_guard.py` | FR-09：实盘复核、used_ratio 透传 | `_FakeBroker` |
| `tests/test_news_freshness_tz.py` | FR-11：跨时区一致 | monkeypatch `TZ` + 冻结时钟 |
| `tests/test_theme_coverage_gate.py` | FR-12：覆盖比例 | 构造 hist 字典 |
| `tests/test_trade_attribution.py` | FR-14：漏斗数字正确 | 造 20 条决策 + 5 笔成交 |

### 5.2 复用与新增的夹具

`tests/conftest.py` 的 `_pin_trading_clock` 已把 `risk_control._now_market` 钉在
2026-10-20 10:30(+8)，Batch 1/2 的时段相关用例直接可用。

需新增到 `tests/conftest.py`（供 4 个文件共用）：

```python
@pytest.fixture()
def quant_env(tmp_path, monkeypatch):        # 与 test_quant_buy_flow.py 同款
    ...

@pytest.fixture()
def fake_broker(quant_env):
    """可控 broker: 现金/持仓/跌停/异常 四个开关."""
    from ai_stock.quant import broker as broker_mod
    fb = _FakeBroker()
    broker_mod.set_broker(fb)
    yield fb
    broker_mod._broker = None                # 复位, 防跨用例串味
```

`_FakeBroker` 需实现 `BrokerAdapter` 的全部抽象方法（connect/disconnect/get_account_info/
get_positions/place_order/cancel_order/query_order），`place_order` 按 `self.mode`
返回 `FILLED`/`REJECTED`/`PENDING`。

### 5.3 运行与禁区

```bash
cd backend && .venv/bin/python -m pytest tests/test_risk_account_metrics.py tests/test_sell_exit_state.py -q
cd backend && .venv/bin/python -m pytest tests/ -q            # 全量
```

**不要复用 `--basetemp`**（旧目录清理会撞沙箱批量删除确认）。

---

## 6. 灰度与上线

### 6.1 上线顺序（不可并行）

```
Batch1(度量) ──→ Batch2(卖出可靠性) ──→ Batch3(卖出质量) ──→ Batch4(择时/实盘) ──→ Batch5(打磨)
                      │                      │
                      └── 观察 5 个交易日 ───┘
```

Batch1 与 Batch2 之间必须留观察期：先确认回撤/浮亏数字正确，再让新的卖出行为跑在上面。

### 6.2 每一步的飞行检查（上生产后 30 分钟内）

| 检查 | 命令/看什么 | 通过标准 |
|---|---|---|
| HWM 是否初始化 | `quant_system_config` 查 `account_hwm` | 值 ≥ 初始资金，不为 0 |
| 是否有伪回撤 | `quant_agent_decision_log` 查 `risk_control/freeze_account` | 部署后 30 分钟内无新增 |
| 退出意图是否正常 | 查 `quant_stock_pool_holding.plan_json` 是否出现 `pending_exit` | 仅在有拒单时出现 |
| 委托笔数是否异常 | `count_orders_today()` | 与部署前同量级，无跳增 |

### 6.3 生产写入配置（部署后执行一次）

```bash
sudo -u www-data bash -c 'set -a; . /deploy/AI_stock/.env; set +a; \
  cd /deploy/AI_stock/backend; .venv/bin/python - <<PY
from ai_stock.quant import db_ops
for k, v in {
    "drawdown_basis": "hwm",
    "rr_missing_policy": "warn",      # 首周观察, Batch 后切 reject
    "sell_retry_enabled": "1",
    "allow_limit_down_sell": "1",
}.items():
    db_ops.set_config_value(k, v)
print(db_ops.get_all_config().get("account_hwm"))
PY'
```

### 6.4 全局回滚（30 秒）

按 §4.2 把运行时键逐个改回回滚值即可，无需回滚代码。唯一需要回滚代码的是 Batch 1 的
`account_snapshot`（若发现 HWM 逻辑本身有误）：`git revert` 该 commit，`account_hwm` 键留着无害。

---

## 7. 实现陷阱清单（全部来自代码事实，不是推测）

1. **`count_orders_today` 无差别计数**（`db_ops.py:520-537`）：挂单/重试一律不落 `trade_log`，
   否则 `max_daily_orders=50` 会被退出重试吃光。
2. **告警按 `alert_type` 静默**（`ops_monitor.py:35-44`）：必须用 `f"sell_blocked:{symbol}"`，
   否则第一只票的告警会把后面所有票的告警静默掉。
3. **`Position.quantity` 是可用量**（`broker.py:96-103`）：MTM 必须用 `total_quantity`。
4. **`session_scope` 每次新 session**：`plan_json` 读写必须走 `mutate_holding_plan` 单 session 原子操作，
   否则 `_update_plan` 与 `pending_exit` 会互相覆盖。
5. **flow 幂等窗口 10 分钟**（`flow_id=f"hold_{symbol}_{window}"`）：`pending_exit` 重试需独立冷却
   （30 分钟），否则每轮扫描都会起 flow、重复打 `pre_trade_check`。
6. **`get_realtime_quote` 有熔断与过期兜底**（`data_service.py:629-658`）：MTM 依赖它不会抛，
   但**降级行情**（`degraded=True` 语义）下 `prev_close` 来自 K 线，数值可信，无需额外判断。
7. **broker 取数失败时 `account_snapshot` 回零**：此时绝不能写 HWM（0 会污染基准），
   已用 `drawdown_basis="unavailable"` 显式标注。
8. **`_ensure_state` 只回放 `filled`**（`broker.py:235-236`）：挂单不落库对状态重建零影响。
9. **循环依赖**：`risk_control` 在模块顶层 import `broker`，所以 `broker` 只能在函数内延迟 import
   `pre_trade_check`，写在模块顶层会 ImportError。
10. **多用户扇出**：`pending_exit` 只作用于 system 账户（引擎持仓）；`fanout_sell` 在各用户内部
    已自带 T+1 校验，不需要也不应该共享同一份退出状态。
11. **`buy_date` 是日历日字符串 `%Y-%m-%d`**（`broker.py:41-49`、`:195-197`），不是交易日序号；
    Huaxing 适配器返回的 `Position.buy_date` 为空串，此时 MTM 一律按昨收计算，不得因此判为数据缺失。
12. **`_parse_risk_reward` 的冒号方向**：现有实现取"大数/小数"（`buy.py:129-135`），
    `1:0.8` 会被读成 1.25 而非 0.8。本计划不改该函数语义（改动会牵动已有测试与历史统计），
    但 `detail` 必须保留 `risk_reward_ratio` 原文，供事后人工核对方向。

---

## 8. 完成定义（DoD）

一个 Batch 算完成，必须同时满足：

1. 代码合入，`tests/` 全量通过（`cd backend && .venv/bin/python -m pytest tests/ -q`）。
2. 每个 FR 至少一条"修复前失败、修复后通过"的反例测试。
3. 生产部署后 30 分钟飞行检查（§6.2）全部通过。
4. 对应的运行时键已写入生产，值与本文件 §4.2 一致。
5. 行为变化写入 `CHANGELOG.md`，包含：改动前后的行为差异、观察指标、回滚键名。
6. 涉及止损/止盈/仓位的 Batch（2、3），附一份模拟盘 10 个交易日的"新旧行为差异报告"：
   触发次数、平均卖出价相对当日收盘的偏离、退出意图平均存续时长。

---

## 9. 实施记录（编码完成后的偏差与遗留）

编码已完成，测试全绿。以下是与本计划的**实际偏差**，逐条说明理由，不留悬念：

1. **`THEME_MIN_HISTORY_COVERAGE` 落在 `theme_registry.GATE_MIN_HISTORY_COVERAGE`**，
   不在 `config.py`。理由：`GATE_MIN_HISTORY_DAYS` 本就在该模块内，闸门阈值集中
   一处才不会出现"改了一个忘了另一个"；该常量也没有跨模块使用方，放 `config.py`
   反而只是搬运。命名沿用同模块的 `GATE_` 前缀。
2. **覆盖不足的降级判定加了"唯一原因"条件**：只有 `len(reasons) == 1`（覆盖不足
   是唯一不合格原因）才降为 `shadow`；成分股数量不足、相关性过低、离散度超标等
   结构性不合格仍判 `rejected`。若不加这一条，"8 只里只有 5 只有历史"会连带把
   "只有 5 只成分股"这类主题也放过闸门。
3. **`_sell_reason_mix` 改为按语义前缀归并**（去掉数字后再截断），而非计划里的
   "按前 12 字归并"。固定字数截断会把 `硬性止损: 浮亏 5.1%` 与 `5.4%` 拆成两个桶，
   而报表恰恰要合并它们 —— 数字是参数，不是分类维度。
4. **额外修掉一个真实 Bug**：`agents/risk.py::handle_force_reduce` 使用了不存在的
   `Position.available`，强制减仓路径一直抛 `AttributeError`（被新用例暴露）。
   改为 `pos.quantity`。这不在 14 条 FR 内，属于顺手修，已单独写进 CHANGELOG。

以下 DoD 项**无法在编码阶段完成**，需要运行时数据，不假装已完成：

- **§8.3 飞行检查 / §8.4 生产写入运行时键**：属部署期动作，需在 `118.25.48.120`
  上执行（命令见 §6.3），本次未执行。
- **§8.6 模拟盘 10 交易日新旧行为差异报告**：需要先跑满 10 个交易日才有数据。
  其中"触发次数"与"卖出原因分布"已可由 `scripts/trade_attribution.py` 直接给出；
  但"平均卖出价相对当日收盘的偏离"需要收盘行情、"退出意图平均存续时长"需要对
  *已成交* 的退出意图留痕（当前 `pending_exit` 在成交时即清除，只保留了失败中的
  那部分）。这两项若确实要统计，需要在 `_execute_sell` 成交时把 `pending_exit`
  的 `first_at` / `attempts` 落到 `trade_log` 的备注字段，属于独立的增量改动，
  不建议与本次整改混在一起评审。

