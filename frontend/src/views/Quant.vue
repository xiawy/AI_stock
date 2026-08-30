<template>
  <div>
    <AppHeader />
    <div class="page">
      <div class="page-header">
        <h2>量化交易</h2>
        <div class="header-actions">
          <el-button :icon="Refresh" :loading="loading" @click="refresh">刷新</el-button>
        </div>
      </div>

      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="当前为模拟盘：买卖在个人模拟账户执行（自动开户、按配置起始资金）。后续对接实盘后，同一买卖入口将直接执行实盘委托。"
        class="hint"
      />

      <!-- 账户总览 -->
      <div class="account-strip panel" v-loading="loadingAccount">
        <div class="stat">
          <div class="stat-label">可用资金</div>
          <div class="stat-value">{{ fmtMoney(account?.cash_balance) }}</div>
        </div>
        <div class="stat">
          <div class="stat-label">持仓市值</div>
          <div class="stat-value">{{ fmtMoney(account?.market_value) }}</div>
        </div>
        <div class="stat">
          <div class="stat-label">总资产</div>
          <div class="stat-value">{{ fmtMoney(account?.total_assets) }}</div>
        </div>
        <div class="stat">
          <div class="stat-label">总收益</div>
          <div class="stat-value" :class="pnlClass(account?.total_return)">
            {{ fmtMoney(account?.total_return) }}（{{ fmtPct(account?.total_return_pct) }}）
          </div>
        </div>
        <div class="stat">
          <div class="stat-label">起始资金</div>
          <div class="stat-value dim">{{ fmtMoney(account?.initial_capital) }}</div>
        </div>
      </div>

      <el-tabs v-model="activeTab" class="tabs" @tab-change="onTabChange">
        <!-- ── 自选池 ─────────────────────────────────────────── -->
        <el-tab-pane label="自选池" name="pool">
          <el-table :data="pool" v-loading="loadingPool" stripe style="width: 100%">
            <el-table-column label="标的" width="170" fixed>
              <template #default="{ row }">
                <div class="stock-cell">
                  <span class="stock-name">{{ row.name || row.symbol }}</span>
                  <span class="stock-code dim">{{ row.symbol }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column prop="industry" label="行业" width="100">
              <template #default="{ row }">{{ row.industry || '—' }}</template>
            </el-table-column>
            <el-table-column label="利好" min-width="190">
              <template #default="{ row }">
                <div class="factor-list">
                  <el-tooltip
                    v-for="(f, i) in (row.bull_factors || []).slice(0, 3)"
                    :key="i"
                    :content="f"
                    placement="top"
                    :show-after="300"
                  >
                    <el-tag size="small" type="danger" effect="plain" class="factor-tag">{{ f }}</el-tag>
                  </el-tooltip>
                  <span v-if="(row.bull_factors || []).length > 3" class="more-count dim">
                    <el-tooltip placement="top" :show-after="300" popper-class="factor-popper">
                      <template #content>
                        <div
                          v-for="(f, i) in (row.bull_factors || []).slice(3)"
                          :key="'rest' + i"
                          class="popper-item"
                        >{{ f }}</div>
                      </template>
                      <el-tag size="small" type="danger" effect="plain" class="factor-tag">+{{ row.bull_factors.length - 3 }}</el-tag>
                    </el-tooltip>
                  </span>
                  <span v-if="!(row.bull_factors || []).length" class="dim">—</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="利空" min-width="190">
              <template #default="{ row }">
                <div class="factor-list">
                  <el-tooltip
                    v-for="(f, i) in (row.bear_factors || []).slice(0, 3)"
                    :key="i"
                    :content="f"
                    placement="top"
                    :show-after="300"
                  >
                    <el-tag size="small" type="success" effect="plain" class="factor-tag">{{ f }}</el-tag>
                  </el-tooltip>
                  <span v-if="(row.bear_factors || []).length > 3" class="more-count dim">
                    <el-tooltip placement="top" :show-after="300" popper-class="factor-popper">
                      <template #content>
                        <div
                          v-for="(f, i) in (row.bear_factors || []).slice(3)"
                          :key="'rest' + i"
                          class="popper-item"
                        >{{ f }}</div>
                      </template>
                      <el-tag size="small" type="success" effect="plain" class="factor-tag">+{{ row.bear_factors.length - 3 }}</el-tag>
                    </el-tooltip>
                  </span>
                  <span v-if="!(row.bear_factors || []).length" class="dim">—</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="入选理由" min-width="220">
              <template #default="{ row }">
                <el-tooltip
                  v-if="row.reason"
                  placement="top"
                  :show-after="300"
                  popper-class="reason-popper"
                >
                  <template #content>
                    <div class="popper-text">{{ row.reason }}</div>
                  </template>
                  <span class="cell-text">{{ row.reason }}</span>
                </el-tooltip>
                <span v-else class="dim">—</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="150" fixed="right">
              <template #default="{ row }">
                <el-button size="small" type="primary" @click="openBuy(row)">买入</el-button>
                <el-button size="small" type="danger" plain @click="removeFromPool(row)">移出</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!pool.length && !loadingPool" description="自选池暂无标的 — 由选股流程自动入选" />
        </el-tab-pane>

        <!-- ── 持仓池 ─────────────────────────────────────────── -->
        <el-tab-pane label="持仓池" name="holdings">
          <el-table :data="holdings" v-loading="loadingAccount" stripe style="width: 100%">
            <el-table-column label="标的" width="170" fixed>
              <template #default="{ row }">
                <div class="stock-cell">
                  <span class="stock-name">{{ row.name || row.symbol }}</span>
                  <span class="stock-code dim">{{ row.symbol }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="持仓(股)" width="110" align="right">
              <template #default="{ row }">
                {{ row.quantity }}
                <div class="dim mini">可卖 {{ row.available_quantity }}</div>
              </template>
            </el-table-column>
            <el-table-column label="成本价" width="95" align="right">
              <template #default="{ row }">{{ fmtNum(row.cost_price) }}</template>
            </el-table-column>
            <el-table-column label="现价" width="95" align="right">
              <template #default="{ row }">{{ fmtNum(row.last_price) }}</template>
            </el-table-column>
            <el-table-column label="浮动盈亏" width="140" align="right">
              <template #default="{ row }">
                <div :class="pnlClass(row.unrealized_pnl)">{{ fmtMoney(row.unrealized_pnl) }}</div>
                <div class="mini" :class="pnlClass(row.unrealized_pnl)">{{ fmtPct(row.unrealized_pnl_pct) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="止损/止盈" width="130" align="right">
              <template #default="{ row }">
                <span v-if="row.stop_loss || row.take_profit">
                  {{ row.stop_loss ? fmtNum(row.stop_loss) : '—' }} /
                  {{ row.take_profit ? fmtNum(row.take_profit) : '—' }}
                </span>
                <span v-else class="dim">—</span>
              </template>
            </el-table-column>
            <el-table-column label="买入理由" min-width="190">
              <template #default="{ row }">
                <el-tooltip v-if="row.entry_reason" :content="row.entry_reason" placement="top">
                  <span class="cell-text">{{ row.entry_reason }}</span>
                </el-tooltip>
                <span v-else class="dim">—</span>
              </template>
            </el-table-column>
            <el-table-column label="交易计划" min-width="190">
              <template #default="{ row }">
                <el-tooltip
                  v-if="planText(row.plan) !== '—'"
                  :content="planText(row.plan)"
                  placement="top"
                  :show-after="300"
                  popper-class="reason-popper"
                >
                  <span class="cell-text">{{ planText(row.plan) }}</span>
                </el-tooltip>
                <span v-else class="dim">—</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="150" fixed="right">
              <template #default="{ row }">
                <el-button size="small" type="primary" @click="openBuy(row, true)">加仓</el-button>
                <el-button size="small" type="warning" plain @click="openSell(row)">减仓</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!holdings.length && !loadingAccount" description="暂无持仓 — 可在自选池买入或等待引擎建仓" />
        </el-tab-pane>

        <!-- ── 交易历史 ───────────────────────────────────────── -->
        <el-tab-pane label="交易历史" name="history">
          <div class="account-strip panel" v-loading="loadingStats">
            <div class="stat">
              <div class="stat-label">完成交易</div>
              <div class="stat-value">{{ stats?.closed_trades ?? 0 }} 笔</div>
            </div>
            <div class="stat">
              <div class="stat-label">胜率</div>
              <div class="stat-value">{{ fmtPct(stats?.win_rate) }}</div>
            </div>
            <div class="stat">
              <div class="stat-label">累计已实现盈亏</div>
              <div class="stat-value" :class="pnlClass(stats?.total_realized_pnl)">
                {{ fmtMoney(stats?.total_realized_pnl) }}
              </div>
            </div>
            <div class="stat">
              <div class="stat-label">单笔平均收益</div>
              <div class="stat-value" :class="pnlClass(stats?.avg_pnl_pct)">
                {{ fmtPct(stats?.avg_pnl_pct) }}
              </div>
            </div>
          </div>

          <el-table :data="trades" v-loading="loadingTrades" stripe style="width: 100%">
            <el-table-column label="时间" width="165">
              <template #default="{ row }">{{ formatTime(row.trade_time) }}</template>
            </el-table-column>
            <el-table-column label="标的" width="150">
              <template #default="{ row }">
                <div class="stock-cell">
                  <span class="stock-name">{{ row.name || row.symbol }}</span>
                  <span class="stock-code dim">{{ row.symbol }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="方向" width="80" align="center">
              <template #default="{ row }">
                <el-tag size="small" :type="row.side === 'buy' ? 'danger' : 'success'">
                  {{ row.side === 'buy' ? '买入' : '卖出' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="成交价" width="90" align="right">
              <template #default="{ row }">{{ fmtNum(row.price) }}</template>
            </el-table-column>
            <el-table-column label="数量(手)" width="90" align="right">
              <template #default="{ row }">{{ (row.quantity / 100).toFixed(0) }}</template>
            </el-table-column>
            <el-table-column label="金额" width="110" align="right">
              <template #default="{ row }">{{ fmtMoney(row.amount) }}</template>
            </el-table-column>
            <el-table-column label="交易依据" min-width="220">
              <template #default="{ row }">
                <el-tooltip v-if="row.reason" :content="row.reason" placement="top">
                  <span class="cell-text">{{ row.reason }}</span>
                </el-tooltip>
                <span v-else class="dim">—</span>
              </template>
            </el-table-column>
            <el-table-column label="结果" width="150" align="right">
              <template #default="{ row }">
                <template v-if="row.side === 'sell'">
                  <div :class="pnlClass(row.realized_pnl)">{{ fmtMoney(row.realized_pnl) }}</div>
                  <div class="mini" :class="pnlClass(row.realized_pnl)">{{ fmtPct(row.pnl_pct) }}</div>
                </template>
                <span v-else class="dim">建仓</span>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!trades.length && !loadingTrades" description="暂无交易记录" />
        </el-tab-pane>
      </el-tabs>
    </div>

    <!-- 买入 / 加仓对话框 -->
    <el-dialog v-model="buyDialog" :title="buyTitle" width="420px">
      <el-form label-width="90px">
        <el-form-item label="标的">
          <span>{{ buyTarget?.name || buyTarget?.symbol }}（{{ buyTarget?.symbol }}）</span>
        </el-form-item>
        <el-form-item label="买入数量">
          <el-input-number v-model="buyLots" :min="1" :max="10000" :step="1" />
          <span class="form-hint">手（1 手 = 100 股，共 {{ buyLots * 100 }} 股）</span>
        </el-form-item>
        <el-form-item label="成交价">
          <span class="dim">按实时行情模拟成交（含滑点与手续费）</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="buyDialog = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="confirmBuy">
          确认{{ buyIsAdd ? '加仓' : '买入' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 减仓对话框 -->
    <el-dialog v-model="sellDialog" title="减仓 / 清仓" width="420px">
      <el-form label-width="90px">
        <el-form-item label="标的">
          <span>{{ sellTarget?.name || sellTarget?.symbol }}（{{ sellTarget?.symbol }}）</span>
        </el-form-item>
        <el-form-item label="卖出比例">
          <el-radio-group v-model="sellPortion">
            <el-radio-button :value="0.25">1/4</el-radio-button>
            <el-radio-button :value="0.5">一半</el-radio-button>
            <el-radio-button :value="1">清仓</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="可卖数量">
          <span class="dim">{{ sellTarget?.available_quantity ?? 0 }} 股（T+1：当日买入不可卖）</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="sellDialog = false">取消</el-button>
        <el-button type="warning" :loading="submitting" @click="confirmSell">确认卖出</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { quantApi } from '../api/quant'
import AppHeader from '../components/AppHeader.vue'

const activeTab = ref('pool')
const loading = ref(false)
const submitting = ref(false)

// ── 账户 / 持仓 ──────────────────────────────────────────
const account = ref(null)
const loadingAccount = ref(false)
const holdings = computed(() => account.value?.holdings || [])

async function loadAccount() {
  loadingAccount.value = true
  try {
    const { data } = await quantApi.myAccount()
    account.value = data
  } catch {
    account.value = null
  } finally {
    loadingAccount.value = false
  }
}

// ── 自选池 ───────────────────────────────────────────────
const pool = ref([])
const loadingPool = ref(false)

async function loadPool() {
  loadingPool.value = true
  try {
    const { data } = await quantApi.pool('active')
    pool.value = data.pool || []
  } catch {
    pool.value = []
  } finally {
    loadingPool.value = false
  }
}

async function removeFromPool(row) {
  try {
    await ElMessageBox.confirm(
      `确定将「${row.name || row.symbol}」移出自选池？`,
      '移出自选池',
      { type: 'warning', confirmButtonText: '移出', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await quantApi.setPoolStatus(row.symbol, 'removed')
    ElMessage.success('已移出自选池')
    await loadPool()
  } catch {
    /* 错误提示由 axios 拦截器统一处理 */
  }
}

// ── 交易历史 ─────────────────────────────────────────────
const trades = ref([])
const stats = ref(null)
const loadingTrades = ref(false)
const loadingStats = ref(false)
const historyLoaded = ref(false)

async function loadHistory() {
  loadingTrades.value = true
  loadingStats.value = true
  try {
    const [t, s] = await Promise.all([
      quantApi.myTrades({ limit: 500 }),
      quantApi.myStats(),
    ])
    trades.value = t.data.trades || []
    stats.value = s.data
  } catch {
    trades.value = []
    stats.value = null
  } finally {
    loadingTrades.value = false
    loadingStats.value = false
    historyLoaded.value = true
  }
}

function onTabChange(tab) {
  if (tab === 'history' && !historyLoaded.value) loadHistory()
}

async function refresh() {
  loading.value = true
  const tasks = [loadAccount(), loadPool()]
  if (activeTab.value === 'history' || historyLoaded.value) tasks.push(loadHistory())
  await Promise.all(tasks)
  loading.value = false
}

// ── 买入 / 加仓 ──────────────────────────────────────────
const buyDialog = ref(false)
const buyTarget = ref(null)
const buyLots = ref(1)
const buyIsAdd = ref(false)

const buyTitle = computed(() => (buyIsAdd.value ? '加仓' : '买入'))

function openBuy(row, isAdd = false) {
  buyTarget.value = row
  buyIsAdd.value = isAdd
  buyLots.value = 1
  buyDialog.value = true
}

async function confirmBuy() {
  submitting.value = true
  try {
    const { data } = await quantApi.buy({
      symbol: buyTarget.value.symbol,
      quantity: buyLots.value * 100,
      reason: buyIsAdd.value ? '手动加仓' : '手动买入(自选池)',
    })
    ElMessage.success(
      `${buyIsAdd.value ? '加仓' : '买入'}成功：${data.quantity} 股 @ ${data.price}，` +
      `手续费 ${fmtMoney(data.fee)}`,
    )
    buyDialog.value = false
    await loadAccount()
  } catch {
    /* 错误提示由 axios 拦截器统一处理 */
  } finally {
    submitting.value = false
  }
}

// ── 减仓 / 清仓 ──────────────────────────────────────────
const sellDialog = ref(false)
const sellTarget = ref(null)
const sellPortion = ref(1)

function openSell(row) {
  sellTarget.value = row
  sellPortion.value = 1
  sellDialog.value = true
}

async function confirmSell() {
  submitting.value = true
  try {
    const { data } = await quantApi.sell({
      symbol: sellTarget.value.symbol,
      portion: sellPortion.value,
      reason: sellPortion.value >= 1 ? '手动清仓' : '手动减仓',
    })
    ElMessage.success(
      `卖出成功：${data.quantity} 股 @ ${data.price}，` +
      `本笔盈亏 ${fmtMoney(data.realized_pnl)}（${fmtPct(data.pnl_pct)}）`,
    )
    sellDialog.value = false
    await loadAccount()
  } catch {
    /* 错误提示由 axios 拦截器统一处理 */
  } finally {
    submitting.value = false
  }
}

// ── 格式化 ───────────────────────────────────────────────
function fmtMoney(n) {
  if (n === null || n === undefined) return '—'
  const sign = n < 0 ? '-' : ''
  return `${sign}¥${Math.abs(n).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`
}
function fmtNum(n) {
  return n === null || n === undefined ? '—' : Number(n).toFixed(2)
}
function fmtPct(n) {
  if (n === null || n === undefined) return '—'
  return `${(n * 100).toFixed(2)}%`
}
function pnlClass(n) {
  if (n === null || n === undefined || n === 0) return 'dim'
  return n > 0 ? 'profit' : 'loss'  // A股习惯: 红涨绿跌
}
function planText(plan) {
  if (!plan || typeof plan !== 'object') return '—'
  const parts = []
  if (plan.stop_loss_pct) parts.push(`止损 ${plan.stop_loss_pct}%`)
  if (plan.take_profit_pct) parts.push(`止盈 ${plan.take_profit_pct}%`)
  if (plan.add_position_conditions) parts.push(`加仓: ${plan.add_position_conditions}`)
  return parts.length ? parts.join(' · ') : '—'
}
function formatTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d) ? iso : d.toLocaleString('zh-CN')
}

onMounted(() => {
  loadAccount()
  loadPool()
})
</script>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
}
.page-header h2 {
  margin: 0;
  font-size: 1.3rem;
  padding-left: 10px;
  border-left: 3px solid var(--brand);
}
.hint {
  margin-bottom: 16px;
}
.tabs {
  margin-top: 18px;
}
.account-strip {
  display: flex;
  gap: 40px;
  flex-wrap: wrap;
  margin-bottom: 16px;
  padding: 14px 20px;
}
.stat-label {
  font-size: 0.78rem;
  color: var(--text-dim);
  margin-bottom: 4px;
}
.stat-value {
  font-size: 1.05rem;
  font-weight: 700;
}
.stock-cell {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
}
.stock-name {
  font-weight: 600;
}
.stock-code {
  font-size: 0.75rem;
}
.factor-list {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.factor-tag {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: help;
}
.cell-text {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  color: var(--text-dim);
  font-size: 0.85rem;
  line-height: 1.5;
  cursor: help;
}
.dim {
  color: var(--text-dim);
}
.mini {
  font-size: 0.75rem;
}
.profit {
  color: #ef4444;  /* 红涨 */
  font-weight: 600;
}
.loss {
  color: #22c55e;  /* 绿跌 */
  font-weight: 600;
}
.form-hint {
  margin-left: 10px;
  color: var(--text-dim);
  font-size: 0.85rem;
}
</style>

<!-- popper 内容 teleport 到 body, 需非 scoped 样式; 限定 popper-class 避免污染全局 -->
<style>
.factor-popper,
.reason-popper {
  max-width: 420px;
  line-height: 1.6;
}
.factor-popper .popper-item {
  padding: 2px 0;
}
.reason-popper .popper-text {
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
