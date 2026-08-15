<template>
  <div>
    <AppHeader />
    <div class="page dashboard">
      <el-row :gutter="16">
        <!-- Left: new analysis form -->
        <el-col :xs="24" :md="10">
          <div class="panel">
            <h3 class="panel-title">新建分析</h3>
            <el-form label-position="top">
              <el-form-item label="股票代码" required>
                <StockSearch v-model="form.ticker" @resolved="onResolved" />
              </el-form-item>
              <el-form-item label="分析日期">
                <el-date-picker
                  v-model="form.tradeDate"
                  type="date"
                  value-format="YYYY-MM-DD"
                  :disabled-date="(d) => d.getTime() > Date.now()"
                  style="width: 100%"
                />
              </el-form-item>
              <el-form-item label="数据起始日期">
                <el-date-picker
                  v-model="form.startDate"
                  type="date"
                  value-format="YYYY-MM-DD"
                  style="width: 100%"
                />
                <div class="field-hint">
                  技术分析回溯到该日期（默认本月第一天）
                </div>
              </el-form-item>

              <el-button
                type="primary"
                size="large"
                class="start-btn"
                :loading="starting"
                :disabled="!form.ticker"
                @click="startAnalysis"
              >
                开始分析
              </el-button>
            </el-form>
          </div>

          <!-- Watchlist -->
          <div class="panel" style="margin-top: 16px">
            <h3 class="panel-title">⭐ 自选股</h3>
            <div class="watch-add">
              <StockSearch v-model="watchInput" @resolved="onWatchResolved" />
              <el-button :disabled="!watchInput" @click="addWatch">添加</el-button>
            </div>
            <div v-if="!watchlist.length" class="empty-hint">暂无自选股</div>
            <div v-else class="watch-list">
              <el-tag
                v-for="item in watchlist"
                :key="item.id"
                closable
                size="large"
                class="watch-tag"
                @close="removeWatch(item.ticker)"
                @click="fillTicker(item.ticker)"
              >
                {{ item.label || item.ticker }}
              </el-tag>
            </div>
          </div>
        </el-col>

        <!-- Right: welcome / kline -->
        <el-col :xs="24" :md="14">
          <div class="panel">
            <template v-if="form.ticker">
              <h3 class="panel-title">
                {{ resolvedLabel || form.ticker }} · 日K线
              </h3>
              <KlineChart :code="form.ticker" :days="120" :height="420" />
            </template>
            <div v-else class="welcome">
              <div class="welcome-emoji">📈</div>
              <div class="welcome-title brand-title">
                <span class="accent">Trading</span>Agents<span class="accent">-Astock</span>
              </div>
              <p class="welcome-desc">
                A股多Agent投研分析系统<br />
                7位AI分析师 → 质量门控 → 多空辩论 → 风控评估 → 最终决策
              </p>
              <div class="welcome-tip">← 在左侧输入股票代码，开始分析</div>
              <div class="welcome-disclaimer">
                ⚠️ 本项目仅供学习研究与技术演示，不构成任何投资建议。<br />
                投资决策请咨询持牌专业机构。
              </div>
            </div>
          </div>

          <!-- Incomplete tasks -->
          <div class="panel" style="margin-top: 16px">
            <h3 class="panel-title">⏳ 未完成任务</h3>
            <div v-if="!incomplete.length" class="empty-hint">暂无未完成任务</div>
            <div v-else class="task-list">
              <el-button
                v-for="entry in incomplete"
                :key="`${entry.ticker}-${entry.trade_date}`"
                class="task-item"
                @click="resumeTask(entry)"
              >
                {{ entry.ticker }} · {{ entry.trade_date }} ·
                {{ statusLabel(entry.status) }}
                <template v-if="entry.checkpoint_step != null">
                  · step {{ entry.checkpoint_step }}
                </template>
              </el-button>
            </div>
          </div>

          <!-- History -->
          <div class="panel" style="margin-top: 16px">
            <h3 class="panel-title">🗂️ 历史记录</h3>
            <div v-if="!history.length" class="empty-hint">暂无历史记录</div>
            <div v-else class="task-list">
              <el-button
                v-for="entry in history"
                :key="entry.path"
                class="task-item"
                @click="openHistory(entry)"
              >
                {{ entry.ticker }} · {{ entry.date }}
              </el-button>
            </div>
          </div>
        </el-col>
      </el-row>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import AppHeader from '../components/AppHeader.vue'
import StockSearch from '../components/StockSearch.vue'
import KlineChart from '../components/KlineChart.vue'
import { analysisApi } from '../api/analysis'
import { historyApi, watchlistApi } from '../api/stocks'

const router = useRouter()

function defaultDates() {
  const now = new Date()
  const iso = (d) => d.toISOString().slice(0, 10)
  return {
    tradeDate: iso(now),
    startDate: iso(new Date(now.getFullYear(), now.getMonth(), 1)),
  }
}

const form = reactive({
  ticker: '',
  ...defaultDates(),
})

const resolvedLabel = ref('')
const starting = ref(false)
const incomplete = ref([])
const history = ref([])
const watchlist = ref([])
const watchInput = ref('')

const lookbackDays = computed(() => {
  const start = new Date(form.startDate)
  const end = new Date(form.tradeDate)
  const days = Math.floor((end - start) / 86400000)
  return Math.max(days, 5)
})

function onResolved(option) {
  resolvedLabel.value = option?.label || ''
}

async function startAnalysis() {
  if (!form.ticker) return
  starting.value = true
  try {
    const { data } = await analysisApi.start({
      ticker: form.ticker,
      trade_date: form.tradeDate,
      lookback_days: lookbackDays.value,
      fresh: true,
    })
    router.push({ name: 'analysis', params: { taskId: data.task_id } })
  } finally {
    starting.value = false
  }
}

async function resumeTask(entry) {
  const { data } = await analysisApi.resumeCheckpoint({
    ticker: entry.ticker,
    trade_date: entry.trade_date,
  })
  router.push({ name: 'analysis', params: { taskId: data.task_id } })
}

function openHistory(entry) {
  router.push({
    name: 'history-report',
    params: { ticker: entry.ticker, tradeDate: entry.date },
  })
}

function statusLabel(status) {
  return { error: '出错', paused: '已暂停', running: '进行中' }[status] || '可继续'
}

function fillTicker(code) {
  form.ticker = code
}

async function onResolvedWatch() {
  /* handled on add */
}

async function addWatch() {
  await watchlistApi.add(watchInput.value)
  ElMessage.success('已添加到自选')
  watchInput.value = ''
  loadWatchlist()
}

async function removeWatch(ticker) {
  await watchlistApi.remove(ticker)
  loadWatchlist()
}

async function loadWatchlist() {
  try {
    const { data } = await watchlistApi.list()
    watchlist.value = data
  } catch {
    /* list is best-effort */
  }
}

async function loadDashboard() {
  try {
    const [inc, hist] = await Promise.all([
      analysisApi.incomplete(),
      historyApi.list(),
    ])
    incomplete.value = inc.data.slice(0, 10)
    history.value = hist.data.slice(0, 20)
  } catch {
    /* sidebar panels are best-effort */
  }
  loadWatchlist()
}

onMounted(loadDashboard)
</script>

<style scoped>
.panel-title {
  margin: 0 0 14px;
  font-size: 1rem;
}
.field-hint {
  color: var(--text-dim);
  font-size: 0.78rem;
  margin-top: 4px;
  line-height: 1.5;
}
.start-btn {
  width: 100%;
  font-weight: 700;
  letter-spacing: 0.2em;
  margin-top: 4px;
}
.watch-add {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}
.watch-tag {
  cursor: pointer;
  margin: 0 8px 8px 0;
}
.watch-list {
  display: flex;
  flex-wrap: wrap;
}
.empty-hint {
  color: var(--text-dim);
  font-size: 0.85rem;
}
.task-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.task-item {
  justify-content: flex-start;
  text-align: left;
}
.welcome {
  text-align: center;
  padding: 40px 16px;
}
.welcome-emoji {
  font-size: 3.5rem;
  margin-bottom: 8px;
}
.welcome-title {
  font-size: 2rem;
}
.welcome-desc {
  color: var(--text-dim);
  line-height: 1.7;
  margin: 12px auto 0;
  max-width: 420px;
}
.welcome-tip {
  margin-top: 22px;
  padding: 10px 18px;
  border: 1px solid var(--border);
  border-radius: 10px;
  color: var(--text-dim);
  display: inline-block;
  font-size: 0.9rem;
}
.welcome-disclaimer {
  margin-top: 22px;
  color: #555;
  font-size: 0.75rem;
  line-height: 1.7;
  border-top: 1px solid #1a1a1a;
  padding-top: 12px;
}
</style>
