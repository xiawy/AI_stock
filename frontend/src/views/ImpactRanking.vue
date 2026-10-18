<template>
  <div>
    <AppHeader />
    <div class="page">
      <div class="page-header">
        <h2>今日新闻榜</h2>
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
        <span>采集新闻：{{ snapshot.total_news_collected }} 条</span>
        <span v-if="typeSummary" class="type-summary">{{ typeSummary }}</span>
      </div>

      <el-table
        v-if="newsItems.length"
        :data="newsItems"
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

        <el-table-column label="标题" min-width="300">
          <template #default="{ row }">
            <div class="title-cell">
              <span class="type-tag" :class="`t-${row.event_type || 'news'}`">
                {{ typeLabel(row.event_type) }}
              </span>
              <span class="title-text">{{ row.title }}</span>
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
            <span v-else class="dim">—</span>
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

        <el-table-column label="多空" width="86" align="center">
          <template #default="{ row }">
            <span class="bias" :class="dirClass(row.bull_bear_bias)">
              {{ biasLabel(row.bull_bear_bias, row.bias_strength) }}
            </span>
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

        <el-table-column label="来源" width="120">
          <template #default="{ row }">
            <span class="src">{{ row.source }}</span>
            <span v-if="row.source_count > 1" class="src-count">×{{ row.source_count }}</span>
          </template>
        </el-table-column>
      </el-table>

      <el-empty v-else-if="!loading" description="暂无新闻数据" />

      <ImpactDetail v-model="detailVisible" :news="selectedNews" />
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowLeft } from '@element-plus/icons-vue'
import { impactApi } from '../api/impact'
import AppHeader from '../components/AppHeader.vue'
import ImpactDetail from '../components/ImpactDetail.vue'

const router = useRouter()
const loading = ref(false)
const snapshot = ref(null)
const newsItems = ref([])
const selectedDate = ref('')
const detailVisible = ref(false)
const selectedNews = ref(null)

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

function biasLabel(bias, strength) {
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
</style>
