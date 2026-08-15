<template>
  <div class="page-container">
    <div class="page-header">
      <h2>今日荐股</h2>
      <div class="header-actions">
        <el-date-picker
          v-model="selectedDate"
          type="date"
          placeholder="选择日期"
          format="YYYY-MM-DD"
          value-format="YYYY-MM-DD"
          @change="loadData"
        />
        <el-button type="primary" :loading="triggering" @click="triggerPipeline">
          手动运行
        </el-button>
      </div>
    </div>

    <div v-if="snapshot" class="snapshot-info">
      <el-tag>{{ snapshot.period === 'AM' ? '上午盘' : '下午盘' }}</el-tag>
      <span>快照时间：{{ formatTime(snapshot.snapshot_time) }}</span>
    </div>

    <div v-if="primary.length" class="section">
      <h3>正选推荐 (Top {{ primary.length }})</h3>
      <StockCard v-for="stock in primary" :key="stock.ticker" :stock="stock" />
    </div>

    <div v-if="alternates.length" class="section">
      <h3>备选 ({{ alternates.length }})</h3>
      <StockCard v-for="stock in alternates" :key="stock.ticker" :stock="stock" />
    </div>

    <el-empty v-else-if="!loading" description="暂无荐股数据">
      <el-button type="primary" @click="triggerPipeline">手动运行流水线</el-button>
    </el-empty>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { recommendationApi } from '../api/recommendation'
import StockCard from '../components/StockCard.vue'

const loading = ref(false)
const triggering = ref(false)
const snapshot = ref(null)
const recommendations = ref([])
const selectedDate = ref('')

const primary = computed(() =>
  recommendations.value.filter((s) => !s.is_alternate),
)
const alternates = computed(() =>
  recommendations.value.filter((s) => s.is_alternate),
)

async function loadData() {
  loading.value = true
  try {
    let res
    if (selectedDate.value) {
      res = await recommendationApi.history(selectedDate.value)
    } else {
      res = await recommendationApi.latest()
    }
    const data = res.data
    snapshot.value = data.snapshot || null
    recommendations.value = data.recommendations || []
  } catch {
    snapshot.value = null
    recommendations.value = []
  } finally {
    loading.value = false
  }
}

async function triggerPipeline() {
  triggering.value = true
  try {
    await recommendationApi.trigger()
    ElMessage.success('流水线已启动，请稍后刷新查看结果')
  } catch {
    ElMessage.error('流水线启动失败')
  } finally {
    triggering.value = false
  }
}

function formatTime(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleString('zh-CN')
}

onMounted(loadData)
</script>

<style scoped>
.page-container {
  max-width: 1200px;
  margin: 0 auto;
  padding: 24px;
}
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
}
.page-header h2 {
  margin: 0;
  font-size: 1.3rem;
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
  margin-bottom: 20px;
  color: var(--text-dim);
  font-size: 0.85rem;
}
.section {
  margin-bottom: 24px;
}
.section h3 {
  font-size: 1.1rem;
  margin-bottom: 12px;
  padding-left: 8px;
  border-left: 3px solid var(--brand, #409eff);
}
</style>
