<template>
  <div>
    <AppHeader />
    <div class="page">
      <div class="page-header">
        <h2>今日热股榜</h2>
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
      <el-tag>自选池热股</el-tag>
      <span v-if="snapshot.generated_at">生成时间：{{ formatTime(snapshot.generated_at) }}</span>
      <span v-else-if="snapshot.as_of">数据日期：{{ snapshot.as_of }}</span>
    </div>

    <div v-if="recommendations.length" class="section">
      <h3>今日热股 (Top {{ recommendations.length }})</h3>
      <StockCard
        v-for="(stock, idx) in recommendations"
        :key="stock.symbol"
        :stock="stock"
        :rank="idx + 1"
      />
    </div>

    <el-empty v-else-if="!loading" description="暂无热股数据（自选池为空），榜单由量化选股流程生成">
      <span class="empty-hint">如需当日数据，可稍后刷新查看；也可选择日期查看历史榜单</span>
    </el-empty>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowLeft } from '@element-plus/icons-vue'
import { recommendationApi } from '../api/recommendation'
import AppHeader from '../components/AppHeader.vue'
import StockCard from '../components/StockCard.vue'

const router = useRouter()
const loading = ref(false)
const snapshot = ref(null)
const recommendations = ref([])
const selectedDate = ref('')

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
.empty-hint {
  color: var(--text-dim);
  font-size: 0.82rem;
}
</style>
