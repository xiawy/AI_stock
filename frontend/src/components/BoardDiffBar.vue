<template>
  <div class="diff-bar" :class="{ empty: !hasPrevious }">
    <span class="label">{{ title }}变化</span>

    <template v-if="hasPrevious">
      <el-tooltip v-if="summary.new.length" placement="top" :content="listText(summary.new)">
        <span class="chip new">新进 {{ summary.new.length }}</span>
      </el-tooltip>
      <el-tooltip v-if="topUp" placement="top" :content="listText(summary.up)">
        <span class="chip up">▲ 最大升 {{ topUp.delta }} 位 · {{ topUp.label }}</span>
      </el-tooltip>
      <el-tooltip v-if="topDown" placement="top" :content="listText(summary.down)">
        <span class="chip down">▼ 最大降 {{ -topDown.delta }} 位 · {{ topDown.label }}</span>
      </el-tooltip>
      <el-tooltip v-if="summary.out.length" placement="top" :content="listText(summary.out)">
        <span class="chip out">掉榜 {{ summary.out.length }}</span>
      </el-tooltip>
      <span v-if="isQuiet" class="chip quiet">无变化</span>
      <span class="ref">对比 {{ previousDate || '上一期' }}</span>
    </template>

    <span v-else class="hint">暂无上一期可比（首次生成或历史快照缺失），只显示当日榜单</span>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  title: { type: String, default: '榜单' },
  summary: { type: Object, default: () => ({}) },
  previousDate: { type: String, default: '' },
})

const hasPrevious = computed(() => props.summary?.has_previous !== false)

/** 上涨最多的一条（delta 为负数表示名次前进的位数） */
const topUp = computed(() => {
  const rows = (props.summary?.up || []).filter((r) => r.delta != null)
  if (!rows.length) return null
  return rows.reduce((a, b) => (b.delta < a.delta ? b : a))
})

/** 下跌最多的一条 */
const topDown = computed(() => {
  const rows = (props.summary?.down || []).filter((r) => r.delta != null)
  if (!rows.length) return null
  return rows.reduce((a, b) => (b.delta > a.delta ? b : a))
})

const isQuiet = computed(
  () =>
    hasPrevious.value &&
    !props.summary?.new?.length &&
    !props.summary?.up?.length &&
    !props.summary?.down?.length &&
    !props.summary?.out?.length,
)

function listText(rows) {
  const labels = (rows || []).map((r) => (typeof r === 'string' ? r : r.label))
  return labels.filter(Boolean).slice(0, 12).join('、') || '—'
}
</script>

<style scoped>
.diff-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 8px 12px;
  margin-bottom: 14px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-panel);
  font-size: 0.8rem;
  color: var(--text-dim);
}
.diff-bar.empty {
  border-style: dashed;
}
.label {
  font-weight: 600;
  color: var(--text-main, inherit);
}
.chip {
  padding: 1px 7px;
  border-radius: 10px;
  border: 1px solid transparent;
  white-space: nowrap;
}
.chip.new {
  color: #f56c6c;
  border-color: #f56c6c55;
  background: #f56c6c14;
}
.chip.up {
  color: #f56c6c;
  border-color: #f56c6c33;
}
.chip.down {
  color: #67c23a;
  border-color: #67c23a33;
}
.chip.out {
  color: var(--text-dim);
  border-color: #88888844;
}
.chip.quiet {
  color: var(--text-dim);
  border-color: #88888833;
}
.ref {
  margin-left: auto;
  font-size: 0.74rem;
  opacity: 0.7;
}
.hint {
  font-size: 0.76rem;
}
</style>
