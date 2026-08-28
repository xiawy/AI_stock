<template>
  <div>
    <div class="tab-toolbar">
      <el-select v-model="statusFilter" size="default" style="width: 170px" @change="loadEvolutions">
        <el-option label="全部状态" value="" />
        <el-option label="待审核" value="pending_review" />
        <el-option label="已批准" value="approved" />
        <el-option label="已拒绝" value="rejected" />
        <el-option label="样本外未达标" value="oos_rejected" />
      </el-select>
      <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      <el-button type="warning" plain :loading="evolving" @click="triggerEvolve">触发止损进化</el-button>
    </div>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="止损/时间止损参数按「训练集搜索 → 验证集确认 → 样本外硬门槛（夏普 ≥ 0.5、最大回撤 ≤ 25%）」时序校验。达标候选以停用状态进入待审核，人工批准后灰度生效；系统永不自动启用规则。"
      class="review-hint"
    />

    <!-- 进化记录 -->
    <div class="section-title">
      进化记录
      <span v-if="pendingCount" class="draft-count">
        <el-tag size="small" type="warning">{{ pendingCount }} 条待审核</el-tag>
      </span>
    </div>
    <el-table :data="evolutions" v-loading="loadingEvo" stripe style="width: 100%">
      <el-table-column label="时间" width="160">
        <template #default="{ row }">{{ formatTime(row.ts) }}</template>
      </el-table-column>
      <el-table-column prop="rule_id" label="候选规则" min-width="190" show-overflow-tooltip />
      <el-table-column label="候选参数" width="150">
        <template #default="{ row }">
          <el-tooltip placement="top">
            <template #content>
              <pre class="metric-pre">{{ JSON.stringify(row.params, null, 2) }}</pre>
            </template>
            <span class="mono">{{ paramText(row.params) }}</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="训练集" min-width="170">
        <template #default="{ row }"><span class="mono">{{ metricText(row.train_metrics) }}</span></template>
      </el-table-column>
      <el-table-column label="验证集" min-width="170">
        <template #default="{ row }"><span class="mono">{{ metricText(row.valid_metrics) }}</span></template>
      </el-table-column>
      <el-table-column label="样本外" min-width="170">
        <template #default="{ row }"><span class="mono">{{ metricText(row.oos_metrics) }}</span></template>
      </el-table-column>
      <el-table-column label="状态" width="120">
        <template #default="{ row }">
          <el-tag size="small" :type="statusTag(row.status).type">{{ statusTag(row.status).text }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="reviewer_note" label="审核备注" min-width="140" show-overflow-tooltip />
      <el-table-column label="操作" width="160" fixed="right">
        <template #default="{ row }">
          <template v-if="row.status === 'pending_review'">
            <el-button size="small" type="success" @click="approveEvo(row)">批准</el-button>
            <el-button size="small" type="danger" @click="rejectEvo(row)">拒绝</el-button>
          </template>
          <span v-else class="dim">—</span>
        </template>
      </el-table-column>
    </el-table>
    <el-empty v-if="!evolutions.length && !loadingEvo" description="暂无进化记录 — 点击「触发止损进化」生成候选" />

    <!-- 策略规则库 -->
    <div class="section-title">策略规则库</div>
    <el-table :data="rules" v-loading="loadingRules" stripe style="width: 100%">
      <el-table-column prop="rule_id" label="规则" min-width="180" show-overflow-tooltip />
      <el-table-column prop="description" label="说明" min-width="220" show-overflow-tooltip />
      <el-table-column label="触发条件" min-width="180">
        <template #default="{ row }"><span class="mono dim">{{ row.condition }}</span></template>
      </el-table-column>
      <el-table-column prop="action" label="动作" width="110" />
      <el-table-column prop="priority" label="优先级" width="80" align="center" />
      <el-table-column label="版本" width="70" align="center">
        <template #default="{ row }">v{{ row.version }}</template>
      </el-table-column>
      <el-table-column label="灰度" width="80" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.gray_scale" size="small" type="info">灰度</el-tag>
          <span v-else class="dim">—</span>
        </template>
      </el-table-column>
      <el-table-column label="单测" width="90" align="center">
        <template #default="{ row }">
          <span class="num">{{ (row.test_case_ids || []).length }}</span>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="80" align="center">
        <template #default="{ row }">
          <el-switch
            :model-value="row.enabled"
            :loading="toggling.has(row.rule_id)"
            @change="(val) => toggleRule(row, val)"
          />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="110" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :loading="testing.has(row.rule_id)" @click="runTest(row)">运行单测</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { quantApi } from '../../api/quant'

const loading = ref(false)
const loadingEvo = ref(false)
const loadingRules = ref(false)
const evolving = ref(false)

const statusFilter = ref('')
const evolutions = ref([])
const rules = ref([])
const toggling = reactive(new Set())
const testing = reactive(new Set())

const pendingCount = computed(
  () => evolutions.value.filter((r) => r.status === 'pending_review').length,
)

// ── 数据加载 ───────────────────────────────────────────────
async function load() {
  loading.value = true
  await Promise.all([loadEvolutions(), loadRules()])
  loading.value = false
}

async function loadEvolutions() {
  loadingEvo.value = true
  try {
    const { data } = await quantApi.evolutions(statusFilter.value)
    evolutions.value = data.evolutions || []
  } catch {
    evolutions.value = []
  } finally {
    loadingEvo.value = false
  }
}

async function loadRules() {
  loadingRules.value = true
  try {
    const { data } = await quantApi.rules()
    rules.value = data.rules || []
  } catch {
    rules.value = []
  } finally {
    loadingRules.value = false
  }
}

// ── 触发进化（默认以自选池活跃标的为样本） ─────────────────
let refreshTimers = []

function clearRefreshTimers() {
  refreshTimers.forEach(clearTimeout)
  refreshTimers = []
}

async function triggerEvolve() {
  let symbols = []
  try {
    const { data } = await quantApi.pool('active')
    symbols = (data.pool || []).map((p) => p.symbol)
  } catch {
    symbols = []
  }
  if (!symbols.length) {
    ElMessage.warning('自选池暂无活跃标的，进化需要至少 1 个有 ≥60 根日K的标的')
    return
  }
  try {
    await quantApi.evolve({ symbols })
    evolving.value = true
    ElMessage.success(`进化任务已启动（样本 ${symbols.length} 支，后台运行）`)
    // 拉行情 + 三段回测需要时间，延迟两次自动刷新
    refreshTimers.push(
      setTimeout(() => { evolving.value = false; loadEvolutions() }, 10_000),
      setTimeout(loadEvolutions, 30_000),
    )
  } catch {
    evolving.value = false
    /* 错误提示由 axios 拦截器统一处理 */
  }
}

// ── 进化记录审核 ───────────────────────────────────────────
async function approveEvo(row) {
  try {
    await ElMessageBox.confirm(
      `批准候选「${row.rule_id}」？批准前会先运行规则单测，全部通过才以灰度模式启用。`,
      '批准进化候选',
      { type: 'warning', confirmButtonText: '批准', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const { data } = await quantApi.approveEvolution(row.id, '前端审核通过')
    ElMessage.success(`已批准，规则 ${data.rule_id} 灰度生效`)
    await load()
  } catch {
    /* 错误提示由 axios 拦截器统一处理 */
  }
}

async function rejectEvo(row) {
  let note = ''
  try {
    const result = await ElMessageBox.prompt(
      `确定拒绝候选「${row.rule_id}」？拒绝后该候选不会启用。`,
      '拒绝进化候选',
      {
        confirmButtonText: '拒绝',
        cancelButtonText: '取消',
        inputPlaceholder: '审核备注（可选）',
        inputValue: '',
        type: 'warning',
      },
    )
    note = result.value || ''
  } catch {
    return
  }
  try {
    await quantApi.rejectEvolution(row.id, note || '前端审核拒绝')
    ElMessage.success('已拒绝')
    await load()
  } catch {
    /* 错误提示由 axios 拦截器统一处理 */
  }
}

// ── 规则库操作 ─────────────────────────────────────────────
async function toggleRule(row, enabled) {
  toggling.add(row.rule_id)
  try {
    await quantApi.updateRule(row.rule_id, { enabled })
    row.enabled = enabled
    ElMessage.success(`规则 ${row.rule_id} 已${enabled ? '启用' : '停用'}`)
  } catch {
    /* 错误提示由 axios 拦截器统一处理 */
  } finally {
    toggling.delete(row.rule_id)
  }
}

async function runTest(row) {
  testing.add(row.rule_id)
  try {
    const { data } = await quantApi.testRule(row.rule_id)
    const results = data.results || []
    const failed = results.filter((r) => !r.passed)
    if (data.passed) {
      ElMessage.success(`单测全部通过（${results.length} 例）`)
    } else {
      ElMessage.warning(`单测未全部通过：${results.length - failed.length}/${results.length}，请检查规则条件`)
    }
  } catch {
    /* 错误提示由 axios 拦截器统一处理 */
  } finally {
    testing.delete(row.rule_id)
  }
}

// ── 展示辅助 ───────────────────────────────────────────────
const STATUS_MAP = {
  pending_review: { text: '待审核', type: 'warning' },
  approved: { text: '已批准', type: 'success' },
  rejected: { text: '已拒绝', type: 'danger' },
  oos_rejected: { text: '样本外未达标', type: 'info' },
}

function statusTag(status) {
  return STATUS_MAP[status] || { text: status || '—', type: 'info' }
}

function paramText(params = {}) {
  if (!params || Object.keys(params).length === 0) return '—'
  const parts = []
  if (params.stop_loss_pct != null) parts.push(`止损 ${(params.stop_loss_pct * 100).toFixed(1)}%`)
  if (params.time_stop_days != null) parts.push(`时间止损 ${params.time_stop_days} 日`)
  return parts.length ? parts.join(' · ') : JSON.stringify(params)
}

function metricText(m = {}) {
  if (!m || !m.samples) return '—'
  const avg = ((m.avg_return || 0) * 100).toFixed(1)
  const dd = ((m.max_drawdown || 0) * 100).toFixed(1)
  return `夏普 ${m.sharpe ?? 0} · 回撤 ${dd}% · 均值 ${avg}%（${m.samples}）`
}

function formatTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d) ? iso : d.toLocaleString('zh-CN')
}

onMounted(load)
onUnmounted(clearRefreshTimers)
</script>

<style scoped>
.tab-toolbar {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-bottom: 12px;
}
.review-hint {
  margin-bottom: 16px;
}
.section-title {
  font-size: 1.05rem;
  font-weight: 700;
  margin: 26px 0 12px;
  padding-left: 10px;
  border-left: 3px solid var(--brand);
}
.draft-count {
  font-size: 0.8rem;
  font-weight: 400;
  margin-left: 8px;
}
.num {
  font-weight: 700;
  color: var(--brand, #409eff);
}
.dim {
  color: var(--text-dim);
}
.mono {
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  font-size: 0.8rem;
}
.metric-pre {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  max-height: 280px;
  overflow: auto;
}
</style>
