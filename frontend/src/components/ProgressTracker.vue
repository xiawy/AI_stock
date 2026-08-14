<template>
  <div class="progress-panel">
    <div class="summary">
      <div class="meta">
        <span class="ticker">{{ status.ticker }}</span>
        <span class="date">{{ status.trade_date }}</span>
        <el-tag v-if="status.is_paused" type="warning" size="small">已暂停</el-tag>
        <el-tag v-else-if="status.is_running" type="success" size="small" effect="dark">
          分析中
        </el-tag>
        <el-tag v-else-if="status.stop_requested" type="info" size="small">停止中</el-tag>
      </div>
      <div class="elapsed">已耗时 {{ elapsedText }}</div>
      <el-progress
        :percentage="doneRatio"
        :stroke-width="10"
        :show-text="false"
        class="bar"
      />
    </div>

    <div class="stages">
      <div
        v-for="stage in status.stages"
        :key="stage.id"
        class="stage"
        :class="stage.status"
      >
        <div class="stage-head">
          <span class="icon">{{ stage.icon }}</span>
          <span class="name">{{ stage.name }}</span>
          <span class="state">
            <template v-if="stage.status === 'done'">✓ 完成</template>
            <template v-else-if="stage.status === 'active'">
              <span class="spinner"></span> 进行中
            </template>
            <template v-else>待处理</template>
          </span>
        </div>
        <el-collapse v-if="stage.report">
          <el-collapse-item :title="`${stage.name} · 阶段产出`">
            <div class="md-body" v-html="renderMarkdown(stage.report)"></div>
          </el-collapse-item>
        </el-collapse>
      </div>
    </div>

    <div class="stats">
      <el-descriptions :column="4" size="small" border>
        <el-descriptions-item label="LLM 调用">{{ status.llm_calls }}</el-descriptions-item>
        <el-descriptions-item label="工具调用">{{ status.tool_calls }}</el-descriptions-item>
        <el-descriptions-item label="Tokens 输入">{{ fmtNum(status.tokens_in) }}</el-descriptions-item>
        <el-descriptions-item label="Tokens 输出">{{ fmtNum(status.tokens_out) }}</el-descriptions-item>
      </el-descriptions>
    </div>

    <div v-if="status.error" class="error-box">
      <el-alert type="error" :closable="false" show-icon>
        <template #title>分析失败</template>
        {{ status.error }}
      </el-alert>
      <p class="hint">已完成阶段已保存在本地断点中；修复模型额度或配置后可继续未完成的部分。</p>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import { stripThinkTags, formatNumber } from '../utils/format'

const props = defineProps({
  status: { type: Object, required: true },
})

const md = new MarkdownIt({ html: false, linkify: true })

function renderMarkdown(text) {
  return md.render(stripThinkTags(String(text || '')))
}

const doneRatio = computed(() => {
  const total = props.status.stages?.length || 1
  const done = props.status.stages?.filter((s) => s.status === 'done').length || 0
  return Math.round((done / total) * 100)
})

const elapsedText = computed(() => {
  const sec = Math.floor(props.status.elapsed || 0)
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${m}:${String(s).padStart(2, '0')}`
})

function fmtNum(n) {
  return formatNumber(n)
}
</script>

<style scoped>
.progress-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.summary .meta {
  display: flex;
  align-items: center;
  gap: 12px;
}
.ticker {
  font-weight: 700;
  font-size: 1.2rem;
}
.date {
  color: var(--text-dim);
}
.elapsed {
  color: var(--text-dim);
  font-size: 0.85rem;
  margin: 6px 0 8px;
}
.stages {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 10px;
}
.stage {
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 14px;
  background: var(--bg-card);
}
.stage.done {
  border-color: rgba(34, 197, 94, 0.4);
}
.stage.active {
  border-color: var(--brand);
  box-shadow: 0 0 0 1px rgba(255, 90, 31, 0.25);
}
.stage-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.stage-head .name {
  font-weight: 600;
  flex: 1;
}
.stage .state {
  font-size: 0.8rem;
  color: var(--text-dim);
}
.stage.done .state {
  color: #22c55e;
}
.stage.active .state {
  color: var(--brand);
}
.spinner {
  display: inline-block;
  width: 10px;
  height: 10px;
  border: 2px solid var(--brand);
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  vertical-align: -1px;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
.error-box .hint {
  color: var(--text-dim);
  font-size: 0.85rem;
}
:deep(.el-collapse) {
  border-top: none;
  margin-top: 6px;
}
</style>
