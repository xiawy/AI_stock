<template>
  <div>
    <AppHeader />
    <div class="page">
      <div class="panel">
        <!-- Live progress while running -->
        <template v-if="!result">
          <div class="controls">
            <el-button
              :disabled="!status?.is_running || status?.is_paused || status?.stop_requested"
              @click="pause"
            >
              暂停
            </el-button>
            <el-button
              :disabled="!status?.is_running || !status?.is_paused"
              @click="resume"
            >
              恢复
            </el-button>
            <el-button type="danger" plain :disabled="stopping" @click="stop">
              停止
            </el-button>
          </div>

          <ProgressTracker v-if="status" :status="status" />
          <el-skeleton v-else :rows="6" animated />
        </template>

        <!-- Final report -->
        <ReportViewer v-else :report="result" />
      </div>
    </div>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import AppHeader from '../components/AppHeader.vue'
import ProgressTracker from '../components/ProgressTracker.vue'
import ReportViewer from '../components/ReportViewer.vue'
import { analysisApi } from '../api/analysis'

const props = defineProps({
  taskId: { type: String, required: true },
})

const router = useRouter()
const status = ref(null)
const result = ref(null)
const stopping = ref(false)
let timer = null

async function poll() {
  try {
    const { data } = await analysisApi.status(props.taskId)
    status.value = data

    if (data.is_complete) {
      clearInterval(timer)
      timer = null
      const res = await analysisApi.result(props.taskId)
      result.value = res.data
    } else if (data.error) {
      clearInterval(timer)
      timer = null
    }
  } catch (err) {
    // 404 — task lost after backend restart; stop polling.
    if (err.response?.status === 404) {
      clearInterval(timer)
      timer = null
      ElMessage.error('任务不存在或已随服务重启丢失')
    }
  }
}

async function pause() {
  await analysisApi.pause(props.taskId)
  poll()
}

async function resume() {
  await analysisApi.resume(props.taskId)
  poll()
}

async function stop() {
  stopping.value = true
  try {
    await analysisApi.stop(props.taskId)
    ElMessage.success('已停止；下一次开始分析会从头生成。')
    router.push('/')
  } finally {
    stopping.value = false
  }
}

onMounted(() => {
  poll()
  timer = setInterval(poll, 2000) // same cadence as the Streamlit rerun loop
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.controls {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
}
</style>
