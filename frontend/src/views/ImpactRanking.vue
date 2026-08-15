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
    </div>

    <el-table
      v-if="newsItems.length"
      :data="newsItems"
      stripe
      highlight-current-row
      @row-click="showDetail"
      style="width: 100%"
    >
      <el-table-column label="排名" width="60" align="center">
        <template #default="{ row }">
          <span class="rank-num">{{ row.rank }}</span>
        </template>
      </el-table-column>
      <el-table-column label="标题" min-width="280">
        <template #default="{ row }">
          <div class="title-cell">
            <el-tag v-if="row.category === 'policy'" size="small" type="danger" class="cat-tag">政策</el-tag>
            {{ row.title }}
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="source" label="来源" width="90" />
      <el-table-column label="行业" width="160">
        <template #default="{ row }">
          <el-tag v-for="ind in (row.industries || []).slice(0, 2)" :key="ind" size="small" class="ind-tag">
            {{ ind }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="综合" width="70" align="center">
        <template #default="{ row }">
          <strong>{{ row.composite_score?.toFixed(1) }}</strong>
        </template>
      </el-table-column>
      <el-table-column label="多空" width="70" align="center">
        <template #default="{ row }">
          <el-tag size="small" :type="getBiasType(row.bull_bear_bias)">
            {{ getBiasLabel(row.bull_bear_bias) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="预期涨幅" width="120" align="center">
        <template #default="{ row }">
          <span v-if="row.expected_gain_high > 0" class="gain-text">
            {{ row.expected_gain_low?.toFixed(1) }}%~{{ row.expected_gain_high?.toFixed(1) }}%
          </span>
          <span v-else class="dim">—</span>
        </template>
      </el-table-column>
    </el-table>

    <el-empty v-else-if="!loading" description="暂无新闻数据" />

    <ImpactDetail v-model="detailVisible" :news="selectedNews" />
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
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

function getBiasType(bias) {
  if (bias === 'bullish') return 'success'
  if (bias === 'bearish') return 'danger'
  return 'info'
}

function getBiasLabel(bias) {
  if (bias === 'bullish') return '偏多'
  if (bias === 'bearish') return '偏空'
  return '中性'
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
}
.rank-num {
  font-weight: 700;
  color: var(--brand, #409eff);
}
.title-cell {
  display: flex;
  align-items: center;
  gap: 6px;
}
.cat-tag, .ind-tag {
  margin-right: 4px;
}
.gain-text {
  color: #f56c6c;
  font-weight: 600;
}
.dim {
  color: var(--text-dim);
}
</style>
