<template>
  <div>
    <AppHeader />
    <div class="page">
      <div class="page-header">
        <h2>行业榜 · 三榜联动</h2>
        <div class="header-actions">
          <el-button type="primary" plain @click="scorecardVisible = true">成绩单</el-button>
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
        <el-tag>榜单日期 {{ snapshot.rank_date }}</el-tag>
        <span v-if="snapshot.created_at">生成时间：{{ formatTime(snapshot.created_at) }}</span>
        <span class="funnel-hint">宏观行业榜（量化选股生命周期优选）→ 中观行业（龙头/热股）→ 微观个股（深度诊股）</span>
        <el-checkbox v-if="diffReady" v-model="sortByChange" size="small" class="sort-toggle">
          按变化排序
        </el-checkbox>
      </div>

      <BoardDiffBar title="行业榜" :summary="diffSummary" :previous-date="diffPrev" />

      <div v-loading="loading" class="tri-board">
        <!-- 第一栏：行业热度榜 -->
        <div class="board">
          <h3>行业热度榜 Top {{ displayRankings.length }}</h3>
          <div v-if="displayRankings.length" class="board-scroll">
            <div
              v-for="row in displayRankings"
              :key="row.rank"
              class="industry-row"
              :class="{ active: selected?.industry === row.industry }"
              @click="selected = row"
            >
              <div class="rank-badge">{{ row.rank }}</div>
              <div class="industry-main">
                <div class="industry-name">
                  {{ row.industry }}
                  <el-tag
                    v-if="row.industry_level"
                    size="small"
                    effect="plain"
                    :type="levelType(row.industry_level)"
                    class="level-tag"
                  >
                    {{ levelLabel(row.industry_level) }}
                  </el-tag>
                  <el-tag v-if="row.stage" size="small" effect="plain" type="success">
                    {{ row.stage }}
                  </el-tag>
                  <el-tag
                    v-if="row.transmission_from"
                    size="small"
                    effect="plain"
                    type="warning"
                  >
                    上游传导·{{ row.transmission_from }}
                  </el-tag>
                  <span
                    v-if="indChange(row)"
                    class="chg"
                    :class="indChange(row).change"
                    :title="indChangeTitle(row)"
                  >
                    {{ indChangeText(row) }}
                  </span>
                </div>
                <div class="industry-meta">
                  <span class="heat">优选级 {{ row.heat_score?.toFixed(1) }}</span>
                  <span v-if="row.days_on_board > 1" class="chain-days">
                    连榜 {{ row.days_on_board }} 天
                  </span>
                  <a class="news-link" title="查看该行业相关新闻" @click.stop="openNews(row)">
                    相关新闻 ↗
                  </a>
                  <a class="news-link" title="新闻 → 行业 → 龙头 → 池内标的" @click.stop="openChain(row.industry)">
                    证据链 ↗
                  </a>
                  <span :class="pctClass(row.change_pct)">{{ pctText(row.change_pct) }}</span>
                  <span :class="flowClass(row.main_net_inflow)">{{ flowText(row.main_net_inflow) }}</span>
                </div>
              </div>
            </div>
          </div>
          <el-empty v-else-if="!loading" description="暂无行业榜数据，榜单由量化选股流程生成" :image-size="60" />
        </div>

        <!-- 第二栏：行业龙头股（联动） -->
        <div class="board">
          <h3>{{ selected ? `${selected.industry} · 龙头股` : '行业龙头' }}</h3>
          <template v-if="selected">
            <div v-if="leaders.length" class="board-scroll">
              <div v-for="s in leaders" :key="s.code" class="stock-row">
                <div class="stock-main">
                  <span class="stock-name">
                    {{ s.name }}
                    <el-tag
                      v-for="lb in s.labels || (s.leader_label ? [s.leader_label] : [])"
                      :key="lb"
                      size="small"
                      effect="plain"
                      :type="lb === '领涨' ? 'danger' : 'warning'"
                    >
                      {{ lb }}
                    </el-tag>
                  </span>
                  <span class="stock-code">{{ s.code }}</span>
                </div>
                <div class="stock-side">
                  <span :class="pctClass(s.change_pct)">{{ pctText(s.change_pct) }}</span>
                  <span class="cap">换手 {{ turnoverText(s.turnover_rate) }}</span>
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

        <!-- 第三栏：热股榜（自选池） -->
        <div class="board">
          <h3>热股榜 Top {{ recommendations.length }}</h3>
          <div v-if="recommendations.length" class="board-scroll">
            <div v-for="(stock, idx) in recommendations" :key="stock.symbol" class="stock-row rec-row">
              <div class="rank-badge">{{ idx + 1 }}</div>
              <div class="rec-main">
                <div class="rec-title">
                  <span class="stock-name">{{ stock.name }}</span>
                  <span class="stock-code">{{ stock.symbol }}</span>
                  <el-tag v-if="stock.industry" size="small" effect="plain">{{ stock.industry }}</el-tag>
                  <el-tag v-if="stock.is_tail" size="small" type="warning" effect="plain" title="鱼尾阶段：买入链路会拦截">
                    鱼尾
                  </el-tag>
                  <el-tag
                    v-else-if="stock.dyn_state && stock.dyn_state !== '正常'"
                    size="small"
                    effect="plain"
                    :type="stock.dyn_state === '增强' ? 'danger' : 'info'"
                  >
                    动态{{ stock.dyn_state }}
                  </el-tag>
                  <span class="score" :title="`动态置信度 ${stock.dyn_confidence?.toFixed(3)}（决定排序与是否被踢出）；入池分 ${stock.confidence?.toFixed(1)}（入池时的静态评估）`">
                    动态 {{ confidenceText(stock.dyn_confidence) }}
                    <em class="pool-score">入池 {{ stock.confidence?.toFixed(1) }}</em>
                  </span>
                </div>
                <p class="rec-logic">{{ stock.reason || stock.stage_judgement }}</p>
              </div>
              <el-button
                size="small"
                type="primary"
                plain
                :loading="diagnosing === stock.symbol"
                @click="startDiagnosis(stock.symbol, stock.name)"
              >
                诊股
              </el-button>
            </div>
          </div>
          <el-empty v-else-if="!loading" description="暂无热股数据（自选池为空）" :image-size="60" />
        </div>
      </div>

      <!-- 行业相关新闻弹窗 -->
      <el-dialog
        v-model="newsVisible"
        :title="`「${newsIndustry}」相关新闻`"
        width="640px"
        top="8vh"
      >
        <div v-loading="newsLoading" class="news-list">
          <div v-for="n in newsItems" :key="n.id" class="news-item" @click="selectedNews = n; detailVisible = true">
            <div class="news-head">
              <el-tag size="small" :type="biasType(n.bull_bear_bias)">
                {{ biasLabel(n.bull_bear_bias) }}
              </el-tag>
              <span class="news-title">{{ n.title }}</span>
              <span v-if="n.composite_score > 0" class="news-score">{{ n.composite_score?.toFixed(1) }}</span>
            </div>
            <div class="news-meta">
              <span v-if="n.source">{{ n.source }}</span>
              <span v-if="n.pub_time">{{ n.pub_time }}</span>
              <span v-if="n.category === 'policy'" class="news-cat">政策</span>
            </div>
            <p v-if="n.debate_summary || n.content" class="news-summary">{{ n.debate_summary || n.content }}</p>
          </div>
          <el-empty
            v-if="!newsLoading && !newsItems.length"
            description="该行业暂无关联新闻"
            :image-size="60"
          />
        </div>
      </el-dialog>

      <!-- 新闻详情弹窗（复用新闻榜的 ImpactDetail） -->
      <ImpactDetail v-model="detailVisible" :news="selectedNews" />

      <BoardScorecard v-model="scorecardVisible" />

      <EvidenceChain v-model="chainVisible" :theme="chainTheme" :date="selectedDate" />
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
import { boardApi } from '../api/board'
import { analysisApi } from '../api/analysis'
import AppHeader from '../components/AppHeader.vue'
import ImpactDetail from '../components/ImpactDetail.vue'
import BoardDiffBar from '../components/BoardDiffBar.vue'
import BoardScorecard from '../components/BoardScorecard.vue'
import EvidenceChain from '../components/EvidenceChain.vue'

const router = useRouter()
const loading = ref(false)
const snapshot = ref(null)
const rankings = ref([])
const recommendations = ref([])
const selectedDate = ref('')
const selected = ref(null)
const diagnosing = ref('')
const newsVisible = ref(false)
const newsLoading = ref(false)
const newsIndustry = ref('')
const newsItems = ref([])
const detailVisible = ref(false)
const selectedNews = ref(null)

// 变化视图
const diffSummary = ref({})
const diffPrev = ref('')
const indChangeMap = ref({})
const sortByChange = ref(false)
const scorecardVisible = ref(false)

// 证据链
const chainVisible = ref(false)
const chainTheme = ref('')

const leaders = computed(() => selected.value?.leader_stocks || [])

const diffReady = computed(() => diffSummary.value?.has_previous === true)

/** 按变化排序：新进优先，其次名次前进最多 */
const displayRankings = computed(() => {
  if (!sortByChange.value) return rankings.value
  const weight = (row) => {
    const c = indChange(row)
    if (!c) return 999
    if (c.change === 'new') return -1000
    if (c.change === 'up') return c.delta_rank
    if (c.change === 'down') return 1000 + c.delta_rank
    return 500
  }
  return [...rankings.value].sort((a, b) => weight(a) - weight(b))
})

function indChange(row) {
  return indChangeMap.value[row.industry] || null
}

function indChangeText(row) {
  const c = indChange(row)
  if (!c) return ''
  if (c.change === 'new') return '新进'
  if (c.change === 'up') return `▲${-c.delta_rank}`
  if (c.change === 'down') return `▼${c.delta_rank}`
  return ''
}

function indChangeTitle(row) {
  const c = indChange(row)
  if (!c) return ''
  if (c.change === 'new') return '本期新进榜'
  if (c.change === 'up') return `上期第 ${c.prev_rank} 名 → 本期第 ${row.rank} 名`
  if (c.change === 'down') return `上期第 ${c.prev_rank} 名 → 本期第 ${row.rank} 名`
  return '名次未变'
}

function openChain(theme) {
  chainTheme.value = theme
  chainVisible.value = true
}

async function loadDiff() {
  try {
    const { data } = await boardApi.industryDiff(selectedDate.value)
    diffSummary.value = data.summary || {}
    diffPrev.value = data.previous_date || ''
    const map = {}
    for (const row of data.rows || []) {
      if (row.industry) map[row.industry] = row
    }
    indChangeMap.value = map
  } catch {
    diffSummary.value = {}
    indChangeMap.value = {}
  }
}

async function loadData() {
  loading.value = true
  try {
    // 行业榜暂无数据（404）不应阻断热股榜展示，故用 allSettled
    const [indRes, recRes] = await Promise.allSettled([
      selectedDate.value ? industryApi.history(selectedDate.value) : industryApi.latest(),
      selectedDate.value ? recommendationApi.history(selectedDate.value) : recommendationApi.latest(),
    ])
    const indData = indRes.status === 'fulfilled' ? indRes.value.data : null
    const recData = recRes.status === 'fulfilled' ? recRes.value.data : null
    // 行业榜响应顶层即榜单元信息（rank_date / created_at），热股快照独立携带
    snapshot.value = indData
      ? { rank_date: indData.rank_date, created_at: indData.created_at }
      : null
    rankings.value = indData?.rankings || []
    recommendations.value = recData?.recommendations || []
    selected.value = rankings.value[0] || null
  } finally {
    loading.value = false
  }
  await loadDiff()
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

/** 查看行业相关新闻：拉取该行业热度来源的新闻列表 */
async function openNews(row) {
  if (!row?.id) return
  newsVisible.value = true
  newsIndustry.value = row.industry
  newsItems.value = []
  newsLoading.value = true
  try {
    const { data } = await industryApi.news(row.id)
    newsItems.value = data.news_items || []
  } catch {
    ElMessage.error('行业新闻加载失败，请稍后重试')
  } finally {
    newsLoading.value = false
  }
}

function biasType(b) {
  return { bullish: 'danger', bearish: 'success', neutral: 'info' }[b] || 'info'
}

function biasLabel(b) {
  return { bullish: '偏多', bearish: '偏空', neutral: '中性' }[b] || '中性'
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

/** 换手率：% */
function turnoverText(v) {
  if (v == null) return '—'
  return `${v.toFixed(1)}%`
}

function confidenceText(v) {
  return v == null ? '—' : v.toFixed(2)
}

function levelType(l) {
  return { concept: 'danger', industry: 'warning' }[l] || 'info'
}

function levelLabel(l) {
  return { concept: '概念板块', industry: '行业板块' }[l] || l
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
@media (max-width: 820px) {
  .tri-board {
    grid-template-columns: 1fr;
  }
}
@media (min-width: 1101px) and (max-width: 1400px) {
  /* 中等宽度屏幕：保持三栏但压缩列间距，避免挤成换行 */
  .tri-board {
    grid-template-columns: 1fr 0.8fr 1fr;
    gap: 10px;
  }
  .industry-name {
    font-size: 0.9rem;
    gap: 4px;
  }
  .industry-meta {
    font-size: 0.8rem;
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
  flex-wrap: wrap;
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
/* 变化标记：新进/名次升降。榜单的核心价值是变化，不是绝对排名 */
.chg {
  flex: none;
  padding: 0 5px;
  border-radius: 3px;
  font-size: 0.7rem;
  font-variant-numeric: tabular-nums;
}
.chg.new {
  color: #f56c6c;
  border: 1px solid #f56c6c66;
  background: #f56c6c14;
  font-weight: 700;
}
.chg.up {
  color: #f56c6c;
  border: 1px solid #f56c6c33;
  background: none;
}
.chg.down {
  color: #67c23a;
  border: 1px solid #67c23a33;
  background: none;
}
.chain-days {
  color: var(--brand);
  font-size: 0.76rem;
}
.sort-toggle {
  margin-left: auto;
}
.pool-score {
  font-style: normal;
  font-size: 0.72rem;
  color: var(--text-dim);
  margin-left: 4px;
}
.news-link {
  color: var(--brand);
  cursor: pointer;
  text-decoration: none;
  border-bottom: 1px dashed transparent;
}
.news-link:hover {
  border-bottom-color: var(--brand);
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

/* 第三栏：热股榜行 */
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

/* 行业新闻弹窗 */
.news-list {
  max-height: 62vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.news-item {
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  cursor: pointer;
  transition: border-color 0.15s;
}
.news-item:hover {
  border-color: var(--brand);
}
.news-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.news-title {
  flex: 1;
  min-width: 0;
  font-weight: 600;
  font-size: 0.9rem;
  line-height: 1.5;
}
.news-score {
  color: var(--brand);
  font-weight: 700;
  font-size: 0.9rem;
  flex-shrink: 0;
}
.news-meta {
  display: flex;
  gap: 12px;
  margin-top: 6px;
  color: var(--text-dim);
  font-size: 0.78rem;
}
.news-cat {
  color: var(--brand);
}
.news-summary {
  margin: 8px 0 0;
  color: var(--text-dim);
  font-size: 0.82rem;
  line-height: 1.6;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
