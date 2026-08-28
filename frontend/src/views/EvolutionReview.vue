<template>
  <div>
    <AppHeader />
    <div class="page">
      <div class="page-header">
        <h2>审核</h2>
        <div class="header-actions">
          <el-button :icon="ArrowLeft" @click="router.push('/')">返回首页</el-button>
        </div>
      </div>

      <el-tabs v-model="activeTab" class="review-tabs">
        <el-tab-pane label="量化" name="quant">
          <QuantEvolution />
        </el-tab-pane>
        <el-tab-pane label="诊股" name="diagnosis" lazy>
          <DiagnosisEvolution />
        </el-tab-pane>
      </el-tabs>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowLeft } from '@element-plus/icons-vue'
import AppHeader from '../components/AppHeader.vue'
import DiagnosisEvolution from '../components/evolution/DiagnosisEvolution.vue'
import QuantEvolution from '../components/evolution/QuantEvolution.vue'

const router = useRouter()
const activeTab = ref('quant')
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
.review-tabs :deep(.el-tabs__item) {
  font-size: 1rem;
  font-weight: 600;
}
</style>
