<template>
  <div>
    <AppHeader />
    <div class="page">
      <div class="page-header">
        <h2>行业榜 · 三榜联动</h2>
        <div class="header-actions">
          <el-button :icon="ArrowLeft" @click="router.push('/')">返回首页</el-button>
          <el-date-picker
            v-model="selectedDate"
            type="date"
            placeholder="选择日期"
            format="YYYY-MM-DD"
            value-format="YYYY-MM-DD"
            @change="loadData"
          />
        </div>
      </div>

      <div v-if="snapshot" class="snapshot-info">
        <el-tag>{{ snapshot.period === 'AM' ? '上午盘' : '下午盘' }}</el-tag>
        <span>快照时间：{{ formatTime(snapshot.snapshot_time) }}</span>
        <span class="funnel-hint">宏观情绪（行业榜）→ 中观行业（龙头/寻龙）→ 微观个股（深度诊股）</span>
      </div>

      <div v-loading="loading" class="tri-board">
        <!-- 第一栏：行业热度榜 -->
        <div class="board">
          <h3>行业热度榜 Top {{ rankings.length }}</h3>
          <div v-if="rankings.length" class="board-scroll">
            <div
              v-for="row in rankings"
              :key="row.rank"
              class="industry-row"
              :class="{ active: selected?.industry === row.industry }"
              @click="selected = row"
            >
              <div class="rank-badge">{{ row.rank }}</div>
              <div class="industry-main">
                <div class="industry-name">
                  {{ row.industry }}
                  <el-tag size="small" :type="ratingType(row.rating)" class="rating-tag">
                    {{ row.rating }}
                  </el-tag>
                </div>
                <div class="industry-meta">
                  <span class="heat">热度 {{ row.heat_score?.toFixed(1) }}</span>
                  <span>{{ row.news_count }} 条新闻</span>
                  <span :class="pctClass(row.change_pct)">{{ pctText(row.change_pct) }}</span>
                  <span :class="flowClass(row.fund_flow_net)">{{ flowText(row.fund_flow_net) }}</span>
                </div>
                <el-tag size="small" :type="resType(row.resonance)">
                  {{ resLabel(row.resonance) }}
                </el-tag>
              </div>
            </div>
          </div>
          <el-empty v-else-if="!loading" description="暂无行业榜数据，榜单由服务端定时更新" :image-size="60" />
        </div>

        <!-- 第二栏：行业龙头股（联动） -->
        <div class="board">
          <h3>{{ selected ? `${selected.industry} · 龙头股` : '行业龙头' }}</h3>
          <template v-if="selected">
            <div v-if="leaders.length" class="board-scroll">
              <div v-for="s in leaders" :key="s.code" class="stock-row">
                <div class="stock-main">
                  <span class="stock-name">{{ s.name }}</span>
                  <span class="stock-code">{{ s.code }}</span>
                </div>
                <div class="stock-side">
                  <span :class="pctClass(s.change_pct)">{{ pctText(s.change_pct) }}</span>
                  <span class="cap">{{ capText(s.market_cap) }}</span>
                  <el-button
                    size="small"
                    type="primary"
                    plain
                    :loading="diagnosing === s.code"
                    @click="startDiagnosis(s.code, s.name)"
                  >
                    诊股
                  </el-button>
                </div>
              </div>
            </div>
            <el-empty v-else description="该行业暂无龙头股数据" :image-size="60" />
          </template>
          <el-empty v-else description="点击左侧行业，查看领涨龙头" :image-size="60" />
        </div>

        <!-- 第三栏：寻龙榜 -->
        <div class="board">
          <h3>寻龙榜 Top {{ primary.length }}</h3>
          <div v-if="primary.length" class="board-scroll">
            <div v-for="stock in primary" :key="stock.ticker" class="stock-row rec-row">
              <div class="rank-badge">{{ stock.rank }}</div>
              <div class="rec-main">
                <div class="rec-title">
                  <span class="stock-name">{{ stock.stock_name }}</span>
                  <span class="stock-code">{{ stock.ticker }}</span>
                  <span class="score">{{ stock.final_score?.toFixed(1) }}</span>
                </div>
                <p class="rec-logic">{{ stock.buy_logic || stock.trigger_event }}</p>
              </div>
              <el-button
                size="small"
                type="primary"
                plain
                :loading="diagnosing === stock.ticker"
                @click="startDiagnosis(stock.ticker, stock.stock_name)"
              >
                诊股
              </el-button>
            </div>
          </div>
          <el-empty v-else-if="!loading" description="暂无寻龙榜数据" :image-size="60" />
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft } from '@element-plus/icons-vue'
import { industryApi } from '../api/industry'
import { recommendationApi } from '../api/recommendation'
import { analysisApi } from '../api/analysis'
import AppHeader from '../components/AppHeader.vue'

const router = useRouter()
const loading = ref(false)
const snapshot = ref(null)
const rankings = ref([])
const recommendations = ref([])
const selectedDate = ref('')
const selected = ref(null)
const diagnosing = ref('')

const primary = computed(() => recommendations.value.filter((s) => !s.is_alternate))
const leaders = computed(() => selected.value?.leader_stocks || [])

async function loadData() {
  loading.value = true
  try {
    // 行业榜暂无数据（404）不应阻断寻龙榜展示，故用 allSettled
    const [indRes, recRes] = await Promise.allSettled([
      selectedDate.value ? industryApi.history(selectedDate.value) : industryApi.latest(),
      selectedDate.value ? recommendationApi.history(selectedDate.value) : recommendationApi.latest(),
    ])
    const indData = indRes.status === 'fulfilled' ? indRes.value.data : null
    const recData = recRes.status === 'fulfilled' ? recRes.value.data : null
    snapshot.value = indData?.snapshot || recData?.snapshot || null
    rankings.value = indData?.rankings || []
    recommendations.value = recData?.recommendations || []
    selected.value = rankings.value[0] || null
  } finally {
    loading.value = false
  }
}

/** 一键深度诊股：启动 TradingAgents 完整多 Agent 分析并跳转进度页 */
async function startDiagnosis(code, name) {
  if (!code) return
  diagnosing.value = code
  try {
    const tradeDate = new Date().toISOString().slice(0, 10)
    const { data } = await analysisApi.start({
      ticker: code,
      trade_date: tradeDate,
      fresh: true,
    })
    router.push({ name: 'analysis', params: { taskId: data.task_id } })
  } catch {
    ElMessage.error(`${name || code} 诊股任务启动失败，请稍后重试`)
  } finally {
    diagnosing.value = ''
  }
}

function pctText(v) {
  if (v == null) return '—'
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
}

function pctClass(v) {
  if (v == null) return 'dim'
  return v >= 0 ? 'up' : 'down'
}

/** 主力净流入：元 → 亿元 */
function flowText(v) {
  if (v == null) return '—'
  const yi = v / 1e8
  return `${yi >= 0 ? '+' : ''}${yi.toFixed(2)}亿`
}

function flowClass(v) {
  if (v == null) return 'dim'
  return v >= 0 ? 'up' : 'down'
}

/** 总市值：元 → 亿元 */
function capText(v) {
  if (v == null) return ''
  return `${(v / 1e8).toFixed(0)}亿`
}

function ratingType(r) {
  return { A: 'danger', B: 'warning', C: 'info' }[r] || 'info'
}

function resType(r) {
  return { strong: 'success', divergence: 'warning', quiet: 'primary', none: 'info' }[r] || 'info'
}

function resLabel(r) {
  return (
    { strong: '热度资金共振', divergence: '热度资金背离', quiet: '资金潜伏', none: '暂无资金数据' }[r] || r
  )
}

function formatTime(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleString('zh-CN')
}

onMounted(loadData)
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
.header-actions {
  display: flex;
  gap: 12px;
  align-items: center;
}
.snapshot-info {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
  color: var(--text-dim);
  font-size: 0.85rem;
}
.funnel-hint {
  color: var(--brand);
  font-size: 0.78rem;
}

.tri-board {
  display: grid;
  grid-template-columns: 1.15fr 0.95fr 1.15fr;
  gap: 16px;
  align-items: start;
}
@media (max-width: 1100px) {
  .tri-board {
    grid-template-columns: 1fr;
  }
}
.board {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
}
.board h3 {
  font-size: 1.02rem;
  margin: 0 0 12px;
  padding-left: 8px;
  border-left: 3px solid var(--brand);
}
.board-scroll {
  max-height: 640px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* 第一栏：行业行 */
.industry-row {
  display: flex;
  gap: 10px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 10px;
  cursor: pointer;
  transition: border-color 0.18s ease, background 0.18s ease;
}
.industry-row:hover {
  border-color: var(--brand);
}
.industry-row.active {
  border-color: var(--brand);
  background: rgba(255, 90, 31, 0.07);
}
.industry-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.industry-name {
  font-weight: 600;
  font-size: 0.98rem;
  display: flex;
  align-items: center;
  gap: 8px;
}
.rating-tag {
  font-weight: 700;
}
.industry-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 0.8rem;
  color: var(--text-dim);
}
.heat {
  color: var(--brand);
  font-weight: 600;
}

/* 股票行（第二/三栏共用） */
.stock-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 10px;
}
.stock-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.stock-name {
  font-weight: 600;
  font-size: 0.95rem;
}
.stock-code {
  color: var(--text-dim);
  font-size: 0.78rem;
}
.stock-side {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 0.82rem;
}
.cap {
  color: var(--text-dim);
}

/* 第三栏：寻龙榜行 */
.rec-row {
  align-items: flex-start;
}
.rec-main {
  flex: 1;
  min-width: 0;
}
.rec-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.score {
  color: var(--brand);
  font-weight: 700;
  font-size: 0.95rem;
  margin-left: auto;
}
.rec-logic {
  margin: 0;
  color: var(--text-dim);
  font-size: 0.8rem;
  line-height: 1.55;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.rank-badge {
  flex-shrink: 0;
  background: var(--brand, #409eff);
  color: #fff;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 0.75rem;
  align-self: center;
}

/* 涨跌颜色：红涨绿跌（与 StockCard 一致） */
.up {
  color: #f56c6c;
  font-weight: 600;
}
.down {
  color: #67c23a;
  font-weight: 600;
}
.dim {
  color: var(--text-dim);
}
</style>
