<template>
  <div>
    <AppHeader />
    <div class="page">
      <div class="panel">
        <el-page-header content="历史分析报告" style="margin-bottom: 16px" @back="$router.back()" />
        <el-skeleton v-if="loading" :rows="8" animated />
        <el-empty v-else-if="error" :description="error" />
        <ReportViewer v-else :report="report" />
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import AppHeader from '../components/AppHeader.vue'
import ReportViewer from '../components/ReportViewer.vue'
import { historyApi } from '../api/stocks'

const props = defineProps({
  ticker: { type: String, required: true },
  tradeDate: { type: String, required: true },
})

const loading = ref(true)
const error = ref('')
const report = ref(null)

onMounted(async () => {
  try {
    const { data } = await historyApi.report(props.ticker, props.tradeDate)
    report.value = data
  } catch (err) {
    error.value = err.response?.data?.detail || '加载失败'
  } finally {
    loading.value = false
  }
})
</script>
