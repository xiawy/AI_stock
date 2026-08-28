<template>
  <div>
    <div class="tab-toolbar">
      <el-button :icon="Refresh" :loading="loading" @click="loadAll">刷新</el-button>
    </div>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="自进化系统只生成「复盘总结 + 策略草稿」，永远不会自动修改策略。草稿需在此页人工审核，批准后才会应用到 custom_strategies/（应用前自动备份原策略）。"
      class="review-hint"
    />

    <el-alert
      v-if="activeJob"
      :type="activeJobStatus === 'running' ? 'warning' : activeJobStatus === 'success' ? 'success' : 'error'"
      :closable="false"
      class="review-hint"
    >
      <template #title>
        复盘任务（{{ activeJob.agent }}）：{{ jobStatusText }}
        <el-button
          v-if="activeJobStatus === 'running'"
          size="small"
          link
          type="primary"
          @click="stopPolling"
        >停止跟踪</el-button>
      </template>
    </el-alert>

    <!-- Agent 状态 -->
    <div class="section-title">Agent 状态</div>
    <el-table :data="agents" v-loading="loadingAgents" stripe style="width: 100%">
      <el-table-column prop="agent" label="Agent" width="150" />
      <el-table-column label="情节记忆" width="120" align="center">
        <template #default="{ row }">
          <el-tooltip :content="`已结算 ${row.episodes_resolved} · 待验证 ${row.episodes_pending}`" placement="top">
            <span class="num">{{ row.episodes_total }}</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="策略文件" min-width="170">
        <template #default="{ row }">
          <el-tag v-if="row.strategy_files?.length" size="small" type="info">
            {{ row.strategy_files.join(', ') }}
          </el-tag>
          <span v-else class="dim">无</span>
        </template>
      </el-table-column>
      <el-table-column label="待审草稿" width="100" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.draft_count" size="small" type="warning">{{ row.draft_count }}</el-tag>
          <span v-else class="dim">0</span>
        </template>
      </el-table-column>
      <el-table-column label="上次复盘" width="120" align="center">
        <template #default="{ row }">
          <span v-if="row.last_learning" class="dim">{{ row.last_learning.date }}</span>
          <span v-else class="dim">—</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="190">
        <template #default="{ row }">
          <el-button
            size="small"
            type="primary"
            :loading="isReviewing(row.agent)"
            @click="triggerReview(row.agent)"
          >复盘</el-button>
          <el-button
            size="small"
            :disabled="!row.last_learning"
            @click="openLearning(row)"
          >查看复盘</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 待审核草稿 -->
    <div class="section-title">
      待审核草稿
      <span v-if="drafts.length" class="draft-count">共 {{ drafts.length }} 个</span>
    </div>
    <el-table
      :data="drafts"
      v-loading="loadingDrafts"
      stripe
      style="width: 100%"
    >
      <el-table-column prop="agent" label="Agent" width="150" />
      <el-table-column prop="filename" label="文件名" min-width="200" />
      <el-table-column label="生成时间" width="180">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="大小" width="80" align="right">
        <template #default="{ row }">{{ row.size }}B</template>
      </el-table-column>
      <el-table-column label="预览" min-width="240">
        <template #default="{ row }">
          <span class="draft-preview">{{ row.preview || '（无预览）' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="220">
        <template #default="{ row }">
          <el-button size="small" @click="openDraft(row)">查看</el-button>
          <el-button size="small" type="success" @click="approveDraft(row)">批准应用</el-button>
          <el-button size="small" type="danger" @click="rejectDraft(row)">拒绝</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-empty v-if="!drafts.length && !loadingDrafts" description="暂无待审核草稿 — 点击 Agent 的「复盘」按钮生成" />

    <!-- 草稿详情 -->
    <el-dialog v-model="draftDialog" :title="draftTitle" width="860px" top="6vh">
      <pre class="draft-content">{{ currentDraft?.content || '加载中…' }}</pre>
      <template #footer>
        <el-button @click="draftDialog = false">关闭</el-button>
        <el-button type="danger" @click="rejectFromDialog">拒绝删除</el-button>
        <el-button type="success" @click="approveFromDialog">批准应用</el-button>
      </template>
    </el-dialog>

    <!-- 复盘总结 -->
    <el-dialog v-model="learningDialog" :title="learningTitle" width="860px" top="6vh">
      <pre class="draft-content">{{ currentLearning || '暂无复盘总结' }}</pre>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { evolutionApi } from '../../api/evolution'

const loading = ref(false)
const loadingAgents = ref(false)
const loadingDrafts = ref(false)

const agents = ref([])
const drafts = ref([])

// ── 复盘任务轮询 ───────────────────────────────────────────
const activeJob = ref(null) // { jobId, agent }
const activeJobStatus = ref('')

let pollTimer = null

const jobStatusText = computed(() => {
  if (activeJobStatus.value === 'running') return '正在生成复盘总结与策略草稿…'
  if (activeJobStatus.value === 'success') return '复盘完成，草稿已进入审核队列'
  if (activeJobStatus.value === 'failed') return '复盘失败'
  return ''
})

function isReviewing(agent) {
  return activeJob.value?.agent === agent && activeJobStatus.value === 'running'
}

async function triggerReview(agent) {
  if (activeJob.value) {
    ElMessage.warning('已有复盘任务进行中，请稍候')
    return
  }
  const { data } = await evolutionApi.triggerReview(agent)
  activeJob.value = { jobId: data.job_id, agent }
  activeJobStatus.value = 'running'
  startPolling()
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(pollJob, 2000)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function pollJob() {
  if (!activeJob.value) return
  try {
    const { data } = await evolutionApi.reviewJob(activeJob.value.jobId)
    activeJobStatus.value = data.status
    if (data.status === 'success') {
      stopPolling()
      ElMessage.success(`「${data.agent}」复盘完成`)
      activeJob.value = null
      await loadAll()
    } else if (data.status === 'failed') {
      stopPolling()
      ElMessage.error(data.error || '复盘失败')
      activeJob.value = null
    }
  } catch {
    stopPolling()
    activeJob.value = null
  }
}

// ── 数据加载 ───────────────────────────────────────────────
async function loadAll() {
  loading.value = true
  await Promise.all([loadAgents(), loadDrafts()])
  loading.value = false
}

async function loadAgents() {
  loadingAgents.value = true
  try {
    const { data } = await evolutionApi.agents()
    agents.value = data.agents || []
  } catch {
    agents.value = []
  } finally {
    loadingAgents.value = false
  }
}

async function loadDrafts() {
  loadingDrafts.value = true
  try {
    const { data } = await evolutionApi.drafts()
    drafts.value = data.drafts || []
  } catch {
    drafts.value = []
  } finally {
    loadingDrafts.value = false
  }
}

// ── 草稿操作 ───────────────────────────────────────────────
const draftDialog = ref(false)
const currentDraft = ref(null)
const draftAgent = ref('')
const draftFilename = ref('')

const draftTitle = computed(() => {
  if (!draftAgent.value) return '草稿详情'
  return `草稿审核：${draftAgent.value} / ${draftFilename.value}`
})

async function openDraft(row) {
  draftAgent.value = row.agent
  draftFilename.value = row.filename
  draftDialog.value = true
  currentDraft.value = null
  try {
    const { data } = await evolutionApi.draft(row.agent, row.filename)
    currentDraft.value = data
  } catch {
    currentDraft.value = null
  }
}

async function approveDraft(row) {
  await confirmApprove(row.agent, row.filename)
}

async function approveFromDialog() {
  await confirmApprove(draftAgent.value, draftFilename.value)
  draftDialog.value = false
}

async function confirmApprove(agent, filename) {
  try {
    await ElMessageBox.confirm(
      '批准后草稿将替换当前策略文件（原策略自动备份到 backup 目录），确定应用？',
      '批准应用草稿',
      { type: 'warning', confirmButtonText: '批准应用', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }
  try {
    const { data } = await evolutionApi.approveDraft(agent, filename)
    ElMessage.success(data.detail || '已批准并应用')
    await loadAll()
  } catch {
    /* 错误提示由 axios 拦截器统一处理 */
  }
}

async function rejectDraft(row) {
  await confirmReject(row.agent, row.filename)
}

async function rejectFromDialog() {
  await confirmReject(draftAgent.value, draftFilename.value)
  draftDialog.value = false
}

async function confirmReject(agent, filename) {
  try {
    await ElMessageBox.confirm(
      `确定拒绝草稿「${filename}」？该草稿将被删除，不会影响当前策略。`,
      '拒绝草稿',
      { type: 'warning', confirmButtonText: '拒绝删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const { data } = await evolutionApi.rejectDraft(agent, filename)
    ElMessage.success(data.detail || '已拒绝并删除')
    await loadAll()
  } catch {
    /* 错误提示由 axios 拦截器统一处理 */
  }
}

// ── 复盘总结 ───────────────────────────────────────────────
const learningDialog = ref(false)
const currentLearning = ref('')
const learningAgent = ref('')

const learningTitle = computed(() =>
  learningAgent.value ? `${learningAgent.value} · 最新复盘总结` : '复盘总结',
)

async function openLearning(row) {
  learningAgent.value = row.agent
  learningDialog.value = true
  currentLearning.value = ''
  try {
    const { data } = await evolutionApi.learning(row.agent)
    currentLearning.value = data.learning?.content || '暂无复盘总结'
  } catch {
    currentLearning.value = '加载失败'
  }
}

function formatTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d) ? iso : d.toLocaleString('zh-CN')
}

onMounted(loadAll)
onUnmounted(stopPolling)
</script>

<style scoped>
.tab-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 12px;
}
.review-hint {
  margin-bottom: 16px;
}
.section-title {
  font-size: 1.05rem;
  font-weight: 700;
  margin: 26px 0 12px;
  padding-left: 10px;
  border-left: 3px solid var(--brand);
}
.draft-count {
  font-size: 0.8rem;
  font-weight: 400;
  color: var(--text-dim);
  margin-left: 8px;
}
.num {
  font-weight: 700;
  color: var(--brand, #409eff);
}
.dim {
  color: var(--text-dim);
}
.draft-preview {
  color: var(--text-dim);
  font-size: 0.85rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  display: block;
}
.draft-content {
  background: #1d1d1d;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 62vh;
  overflow: auto;
  margin: 0;
}
</style>
