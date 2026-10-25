<template>
  <div>
    <AppHeader />
    <div class="page">
      <div class="page-header">
        <h2>今日新闻榜</h2>
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
        <el-tag>{{ snapshot.period === 'AM' ? '上午盘' : '下午盘' }}</el-tag>
        <span>快照时间：{{ formatTime(snapshot.snapshot_time) }}</span>
        <span>采集新闻：{{ snapshot.total_news_collected }} 条</span>
        <span v-if="typeSummary" class="type-summary">{{ typeSummary }}</span>
        <span v-if="newsItems.length" class="coverage" :class="{ empty: themedCount === 0 }">
          可交易信号 {{ themedCount }}/{{ newsItems.length }}
          <el-tooltip placement="top" :content="coverageHint">
            <span class="coverage-mark">?</span>
          </el-tooltip>
        </span>
        <el-checkbox v-if="diffReady" v-model="sortByChange" size="small" class="sort-toggle">
          按变化排序
        </el-checkbox>
      </div>

      <BoardDiffBar title="新闻榜" :summary="diffSummary" :previous-date="diffPrev" />

      <el-table
        v-if="displayItems.length"
        :data="displayItems"
        stripe
        highlight-current-row
        @row-click="showDetail"
        style="width: 100%"
      >
        <el-table-column label="排名" width="58" align="center">
          <template #default="{ row }">
            <span class="rank-num" :class="{ hot: row.rank <= 3 }">{{ row.rank }}</span>
          </template>
        </el-table-column>

        <el-table-column label="变化" width="80" align="center">
          <template #default="{ row }">
            <span v-if="changeOf(row)" class="chg" :class="changeOf(row).change">
              {{ changeText(changeOf(row)) }}
            </span>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>

        <el-table-column label="标题" min-width="300">
          <template #default="{ row }">
            <div class="title-cell">
              <span class="type-tag" :class="`t-${row.event_type || 'news'}`">
                {{ typeLabel(row.event_type) }}
              </span>
              <span class="title-text">{{ row.title }}</span>
              <el-tooltip placement="top" :content="assessHint(row)">
                <span class="assess-tag" :class="assessClass(row.assess_source)">
                  {{ assessLabel(row.assess_source) }}
                </span>
              </el-tooltip>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="影响行业（细分赛道）" min-width="230">
          <template #default="{ row }">
            <template v-if="(row.industries || []).length">
              <span
                v-for="ind in row.industries.slice(0, 2)"
                :key="ind"
                class="ind-chip"
                :class="dirClass(impactOf(row, ind).direction)"
              >
                {{ ind }}
                <em>{{ dirArrow(impactOf(row, ind).direction) }}</em>
              </span>
            </template>
            <el-tooltip
              v-else
              placement="top"
              :content="noThemeHint(row)"
            >
              <span class="dim no-theme">无对应细分</span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="重要度" width="86" align="center">
          <template #default="{ row }">
            <span v-if="row.importance > 0" class="imp" :class="{ key: row.importance >= 8 }">
              {{ row.importance.toFixed(1) }}
            </span>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>

        <el-table-column label="多空" width="92" align="center">
          <template #default="{ row }">
            <el-tooltip placement="top" :content="biasHint(row)" :disabled="!row.bull_bear_bias">
              <span class="bias" :class="dirClass(row.bull_bear_bias)">
                {{ biasLabel(row.bull_bear_bias, row.bias_strength, row.bias_scope) }}
                <em v-if="row.bias_scope === 'market'" class="scope-mark">整体</em>
              </span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="预期影响" width="126" align="center">
          <template #default="{ row }">
            <span v-if="hasGain(row)" class="gain" :class="dirClass(row.bull_bear_bias)">
              {{ fmtGain(row.expected_gain_low) }}~{{ fmtGain(row.expected_gain_high) }}%
            </span>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>

        <el-table-column label="置信度" width="82" align="center">
          <template #default="{ row }">
            <span v-if="row.confidence > 0" class="conf" :class="confClass(row.confidence)">
              {{ confLabel(row.confidence) }}
            </span>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>

        <el-table-column label="来源" width="130">
          <template #default="{ row }">
            <span class="src">{{ row.source }}</span>
            <el-tooltip
              v-if="row.source_count > 1"
              content="同一事件被多家媒体报道，榜单只占一个榜位；点击查看是哪几家"
              placement="top"
            >
              <span class="src-count clickable" @click.stop="showCluster(row)">×{{ row.source_count }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>

      <el-empty v-else-if="!loading" description="暂无新闻数据" />

      <ImpactDetail v-model="detailVisible" :news="selectedNews" />

      <BoardScorecard v-model="scorecardVisible" />

      <el-dialog v-model="clusterVisible" :title="`同事件多源 · ${clusterTitle}`" width="560px">
        <div v-loading="clusterLoading">
          <div v-if="clusterMembers.length" class="cluster-list">
            <div v-for="(m, i) in clusterMembers" :key="i" class="cluster-row">
              <span class="src-name">{{ m.source || '未标来源' }}</span>
              <span class="cluster-title">{{ m.title }}</span>
              <span v-if="m.is_representative" class="rep-tag">榜位</span>
            </div>
          </div>
          <p v-else-if="!clusterLoading" class="cluster-empty">
            该快照没有保存多源明细（此表在本次上线后才开始写入，历史快照无数据）。
            <br />
            榜单只显示 ×{{ clusterCount }} 是聚类计数，明细需要重新生成快照后才有。
          </p>
        </div>
      </el-dialog>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft } from '@element-plus/icons-vue'
import { impactApi } from '../api/impact'
import { boardApi } from '../api/board'
import AppHeader from '../components/AppHeader.vue'
import ImpactDetail from '../components/ImpactDetail.vue'
import BoardDiffBar from '../components/BoardDiffBar.vue'
import BoardScorecard from '../components/BoardScorecard.vue'

const router = useRouter()
const loading = ref(false)
const snapshot = ref(null)
const newsItems = ref([])
const selectedDate = ref('')
const detailVisible = ref(false)
const selectedNews = ref(null)

// 变化视图
const diffSummary = ref({})
const diffPrev = ref('')
const changeMap = ref({})
const sortByChange = ref(false)
const scorecardVisible = ref(false)

// 同事件多源
const clusterVisible = ref(false)
const clusterLoading = ref(false)
const clusterMembers = ref([])
const clusterTitle = ref('')
const clusterCount = ref(0)

const TYPE_LABELS = {
  policy: '政策',
  industry: '行业',
  company: '公司',
  capital: '资金',
  overseas: '海外',
}

const typeSummary = computed(() => {
  const counts = {}
  for (const n of newsItems.value) {
    const t = n.event_type || 'news'
    counts[t] = (counts[t] || 0) + 1
  }
  const parts = Object.entries(counts).map(([t, c]) => `${typeLabel(t)}${c}`)
  return parts.length ? parts.join(' · ') : ''
})

// 真正能落到 L3 赛道 + 有方向 + 有幅度的条目（bias_scope=theme）
const themedCount = computed(
  () => newsItems.value.filter((n) => (n.industries || []).length && n.bias_scope !== 'market').length,
)

const coverageHint = computed(() =>
  themedCount.value === 0
    ? '本份快照的新闻均为政策/资金/公司公告类，没有落到 active 的 L3 赛道（说不出受益环节），按口径不套用大行业。行业与幅度列为空是判断结果，不是数据缺失。'
    : '可交易信号 = 能落到 active 的 L3 赛道、给出方向与预期幅度的条目。其余为宏观背景/事件类，保留在榜但不给行业映射。',
)

/** 有上一期对比时，"变化"列与排序开关才有意义 */
const diffReady = computed(() => diffSummary.value?.has_previous === true)

/** 按变化幅度排序：新进优先，其次名次上升最多，最后按原排名 */
const displayItems = computed(() => {
  if (!sortByChange.value) return newsItems.value
  const weight = (row) => {
    const c = changeOf(row)
    if (!c) return 999
    if (c.change === 'new') return -1000
    if (c.change === 'up') return c.delta_rank  // 负数，越小越靠前
    if (c.change === 'down') return 1000 + c.delta_rank
    return 500
  }
  return [...newsItems.value].sort((a, b) => weight(a) - weight(b))
})

function changeOf(row) {
  return changeMap.value[row.cluster_id || row.title] || null
}

function changeText(c) {
  if (c.change === 'new') return '新进'
  if (c.change === 'up') return `▲${-c.delta_rank}`
  if (c.change === 'down') return `▼${c.delta_rank}`
  return '—'
}

function assessLabel(src) {
  if (src === 'llm') return '模型'
  if (src === 'rule') return '规则'
  return '未标'
}

function assessClass(src) {
  if (src === 'llm') return 'llm'
  if (src === 'rule') return 'rule'
  return 'unknown'
}

function assessHint(row) {
  if (row.assess_source === 'llm') {
    return '本条由模型评估：事件类型、重要度、主题方向与预期幅度都来自 LLM。'
  }
  if (row.assess_source === 'rule') {
    return '本条由规则兜底：LLM 不可用或超出本批配额，置信度通常只有 0.35，读结论时要打折。'
  }
  return '这条快照的评估来源未记录（重构前落库的旧数据）。'
}

async function showCluster(row) {
  const sid = snapshot.value?.id
  if (!sid || !row.cluster_id) return
  clusterTitle.value = row.title
  clusterCount.value = row.source_count || 0
  clusterMembers.value = []
  clusterVisible.value = true
  clusterLoading.value = true
  try {
    const { data } = await boardApi.cluster(sid, row.cluster_id)
    clusterMembers.value = data.members || []
  } catch {
    clusterMembers.value = []
  } finally {
    clusterLoading.value = false
  }
}

async function loadDiff() {
  try {
    const { data } = await boardApi.impactDiff(selectedDate.value)
    diffSummary.value = data.summary || {}
    diffPrev.value = data.previous_date || ''
    const map = {}
    for (const row of data.rows || []) {
      if (row.key) map[row.key] = row
    }
    changeMap.value = map
  } catch {
    diffSummary.value = {}
    changeMap.value = {}
  }
}

function noThemeHint(row) {
  const byType = {
    policy: '政策面信息，但说不出受益的具体 L3 赛道（改动的是规则而非量价），按口径不套用大行业。',
    capital: '资金面/交易面信息，对行业没有收入端的传导链。',
    overseas: '海外事件，A 股无直接对标环节；或只是商品/汇率价格本身。',
    company: '单一个股事件，未构成行业级传导。',
    industry: '行业景气信息，但未落到 active 的 L3 赛道。',
  }
  return byType[row.event_type] || '未落到 active 的 L3 赛道。'
}

function biasHint(row) {
  if (!row.bull_bear_bias || row.bull_bear_bias === 'neutral') return ''
  if (row.bias_scope === 'market') {
    return '整体方向：只对市场情绪/指数，未落到具体赛道，因此不给强度与幅度。'
  }
  return '主题级方向：针对左侧列出的 L3 赛道。'
}

async function loadData() {
  loading.value = true
  try {
    let res
    if (selectedDate.value) {
      res = await impactApi.history(selectedDate.value)
    } else {
      res = await impactApi.latest()
    }
    const data = res.data
    snapshot.value = data.snapshot || null
    newsItems.value = data.news_items || []
  } catch {
    snapshot.value = null
    newsItems.value = []
  } finally {
    loading.value = false
  }
  // 变化视图独立加载: 失败不影响榜单主体（它只是增强信息）
  await loadDiff()
}

function showDetail(row) {
  selectedNews.value = row
  detailVisible.value = true
}

function formatTime(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleString('zh-CN')
}

function typeLabel(t) {
  if (TYPE_LABELS[t]) return TYPE_LABELS[t]
  return '资讯'
}

function impactOf(row, name) {
  const list = row.industry_impacts || []
  return list.find((i) => i.name === name) || {}
}

function dirClass(direction) {
  if (direction === 'bullish') return 'up'
  if (direction === 'bearish') return 'down'
  return 'flat'
}

function dirArrow(direction) {
  if (direction === 'bullish') return '↑'
  if (direction === 'bearish') return '↓'
  return '→'
}

function biasLabel(bias, strength, scope) {
  // market 级方向不给强度（"整体偏多"），theme 级才按 1~3 显示强弱
  if (scope === 'market') {
    if (bias === 'bullish') return '偏多'
    if (bias === 'bearish') return '偏空'
    return '中性'
  }
  if (bias === 'bullish') return ['', '弱多', '中多', '强多'][strength] || '偏多'
  if (bias === 'bearish') return ['', '弱空', '中空', '强空'][strength] || '偏空'
  return '中性'
}

function hasGain(row) {
  return row.expected_gain_high !== 0 || row.expected_gain_low !== 0
}

function fmtGain(v) {
  if (v === undefined || v === null) return '0.0'
  const n = Number(v)
  return `${n > 0 ? '+' : ''}${n.toFixed(1)}`
}

function confLabel(c) {
  if (c > 0.7) return '高'
  if (c >= 0.4) return '中'
  return '低'
}

function confClass(c) {
  if (c > 0.7) return 'high'
  if (c >= 0.4) return 'mid'
  return 'low'
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
.snapshot-info {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
  color: var(--text-dim);
  font-size: 0.85rem;
  flex-wrap: wrap;
}
.type-summary {
  color: var(--text-dim);
  opacity: 0.85;
}
/* 主题覆盖率：整榜零覆盖时标红，直接回答"为什么这列全空" */
.coverage {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0 6px;
  border-radius: 3px;
  border: 1px solid var(--border-color, rgba(128, 128, 128, 0.3));
  font-variant-numeric: tabular-nums;
}
.coverage.empty {
  color: #e6a23c;
  border-color: #e6a23c66;
}
.coverage-mark {
  display: inline-block;
  width: 13px;
  height: 13px;
  line-height: 13px;
  text-align: center;
  border-radius: 50%;
  font-size: 0.66rem;
  border: 1px solid currentColor;
  opacity: 0.7;
  cursor: help;
}
.rank-num {
  font-weight: 700;
  color: var(--text-dim);
}
.rank-num.hot {
  color: var(--brand, #409eff);
}
.title-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}
.title-text {
  line-height: 1.45;
}
.type-tag {
  flex: none;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 0.72rem;
  border: 1px solid transparent;
  white-space: nowrap;
}
.t-policy { color: #f56c6c; border-color: #f56c6c55; background: #f56c6c14; }
.t-industry { color: #e6a23c; border-color: #e6a23c55; background: #e6a23c14; }
.t-company { color: #409eff; border-color: #409eff55; background: #409eff14; }
.t-capital { color: #67c23a; border-color: #67c23a55; background: #67c23a14; }
.t-overseas { color: #9b7cf0; border-color: #9b7cf055; background: #9b7cf014; }
.ind-chip {
  display: inline-block;
  margin: 2px 6px 2px 0;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 0.78rem;
  border: 1px solid transparent;
  white-space: nowrap;
}
.ind-chip em {
  font-style: normal;
  font-weight: 700;
  margin-left: 2px;
}
.up { color: #f56c6c; border-color: #f56c6c55; background: #f56c6c12; }
.down { color: #67c23a; border-color: #67c23a55; background: #67c23a12; }
.flat { color: var(--text-dim); border-color: #88888844; background: transparent; }
.imp {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}
.imp.key {
  color: #f56c6c;
}
.bias {
  font-size: 0.8rem;
  padding: 1px 6px;
  border-radius: 3px;
  border: 1px solid transparent;
}
.gain {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.scope-mark {
  font-style: normal;
  margin-left: 2px;
  font-size: 0.66rem;
  opacity: 0.65;
}
.conf {
  font-size: 0.8rem;
}
.conf.high { color: #67c23a; }
.conf.mid { color: #e6a23c; }
.conf.low { color: var(--text-dim); }
.src { font-size: 0.82rem; }
.src-count {
  margin-left: 4px;
  font-size: 0.72rem;
  color: var(--brand, #409eff);
}
.dim {
  color: var(--text-dim);
}
/* 无主题是"有意的空"，不是缺失数据：用虚线下划线表明可悬停看解释 */
.no-theme {
  font-size: 0.78rem;
  border-bottom: 1px dashed var(--text-dim);
  cursor: help;
}
/* 评估来源徽章：降级必须可见（规则兜底的置信度只有 0.35，读结论要打折） */
.assess-tag {
  flex: none;
  margin-left: 6px;
  padding: 0 4px;
  border-radius: 2px;
  font-size: 0.68rem;
  cursor: help;
}
.assess-tag.llm {
  color: #67c23a;
  border: 1px solid #67c23a55;
  background: #67c23a12;
}
.assess-tag.rule {
  color: #e6a23c;
  border: 1px solid #e6a23c66;
  background: #e6a23c14;
}
.assess-tag.unknown {
  color: var(--text-dim);
  border: 1px solid var(--border-color, rgba(128, 128, 128, 0.35));
}

/* 变化列 */
.chg {
  font-size: 0.76rem;
  font-variant-numeric: tabular-nums;
}
.chg.new {
  color: #f56c6c;
  font-weight: 700;
}
.chg.up {
  color: #f56c6c;
  border: none;
  background: none;
}
.chg.down {
  color: #67c23a;
  border: none;
  background: none;
}
.chg.same {
  color: var(--text-dim);
}
.sort-toggle {
  margin-left: auto;
}
.src-count.clickable {
  cursor: pointer;
  text-decoration: underline dotted;
}

/* 同事件多源弹窗 */
.cluster-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 52vh;
  overflow-y: auto;
}
.cluster-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 0.84rem;
}
.src-name {
  flex: none;
  min-width: 84px;
  color: var(--brand, #409eff);
  font-weight: 600;
  font-size: 0.78rem;
}
.cluster-title {
  flex: 1;
  min-width: 0;
  line-height: 1.5;
}
.rep-tag {
  flex: none;
  padding: 0 5px;
  border-radius: 3px;
  font-size: 0.68rem;
  color: var(--brand, #409eff);
  border: 1px solid currentColor;
}
.cluster-empty {
  color: var(--text-dim);
  font-size: 0.82rem;
  line-height: 1.7;
}
</style>
