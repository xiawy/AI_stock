<template>
  <el-card class="stock-card" shadow="hover">
    <div class="card-header">
      <div class="rank-badge">#{{ rank }}</div>
      <div class="stock-info">
        <span class="stock-name">{{ stock.name }}</span>
        <span class="stock-code">{{ stock.symbol }}</span>
        <el-tag v-if="stock.industry" size="small" type="info">{{ stock.industry }}</el-tag>
        <el-tag size="small" :type="statusType">{{ statusLabel }}</el-tag>
        <el-tag v-if="stock.is_tail" size="small" type="warning" effect="plain" :title="TAIL_HINT">
          鱼尾
        </el-tag>
        <el-tag
          v-else-if="stock.dyn_state && stock.dyn_state !== '正常'"
          size="small"
          effect="plain"
          :type="stock.dyn_state === '增强' ? 'danger' : 'info'"
        >
          动态{{ stock.dyn_state }}
        </el-tag>
        <el-tag v-if="changeTag" size="small" effect="plain" :type="changeTag.type">
          {{ changeTag.text }}
        </el-tag>
      </div>
      <div class="scores" :title="SCORE_HINT">
        <span class="dyn">动态 {{ confidenceText(stock.dyn_confidence) }}</span>
        <span class="pool">入池 {{ stock.confidence != null ? stock.confidence.toFixed(1) : '—' }}</span>
      </div>
    </div>

    <div class="card-body">
      <div v-if="stock.stage_judgement" class="field-line">
        <span class="field-label">阶段研判：</span>{{ stock.stage_judgement }}
      </div>

      <div v-if="stock.reason" class="field-line reason">
        <span class="field-label">入选理由：</span>{{ stock.reason }}
      </div>

      <div v-if="bullFactors.length" class="factors">
        <span class="field-label">看多因素：</span>
        <el-tag
          v-for="(f, i) in bullFactors"
          :key="`bull-${i}`"
          size="small"
          effect="plain"
          class="factor-tag"
        >
          {{ f }}
        </el-tag>
      </div>

      <div v-if="stock.rise_trigger" class="field-line">
        <span class="field-label">上涨触发：</span>{{ stock.rise_trigger }}
      </div>

      <div v-if="riskTags.length" class="factors">
        <span class="field-label">风险提示：</span>
        <el-tag
          v-for="(r, i) in riskTags"
          :key="`risk-${i}`"
          size="small"
          type="danger"
          effect="plain"
          class="factor-tag"
        >
          {{ r }}
        </el-tag>
      </div>

      <el-collapse v-if="stock.report" class="report-collapse">
        <el-collapse-item title="选股报告">
          <p class="report-text">{{ stock.report }}</p>
        </el-collapse-item>
      </el-collapse>
    </div>
  </el-card>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  stock: { type: Object, required: true },
  rank: { type: Number, default: 0 },
  change: { type: Object, default: null },
})

const SCORE_HINT =
  '动态置信度（0-1）每轮由置信度维护官增强/衰减，决定榜内排序，也是买入候选队列的排队依据；'
  + '入池分（0-10）是入池时的静态评估，之后不再变化。两个分差异大＝入池看好但现在在衰减。'
const TAIL_HINT = '鱼尾阶段：买入链路会拦截该标的，榜上不再一视同仁'

const changeTag = computed(() => {
  const c = props.change
  if (!c) return null
  if (c.change === 'new') return { text: '新进', type: 'danger' }
  if (c.change === 'up') return { text: `▲${-c.delta_rank}`, type: 'danger' }
  if (c.change === 'down') return { text: `▼${c.delta_rank}`, type: 'info' }
  return null
})

const bullFactors = computed(() => props.stock.bull_factors || [])
const riskTags = computed(() => props.stock.risk_tags || [])

const statusType = computed(() => {
  const s = props.stock.status
  if (s === 'active') return 'success'
  if (s === 'removed') return 'danger'
  return 'info'
})

const statusLabel = computed(
  () =>
    ({ active: '观察中', removed: '已移出', expired: '已过期', bought: '已买入' }[
      props.stock.status
    ] || props.stock.status || '观察中'),
)

function confidenceText(v) {
  return v == null ? '—' : v.toFixed(2)
}
</script>

<style scoped>
.stock-card {
  margin-bottom: 16px;
  border-left: 3px solid var(--brand, #409eff);
}
.card-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.rank-badge {
  background: var(--brand, #409eff);
  color: #fff;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 0.85rem;
  flex-shrink: 0;
}
.stock-info {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  flex-wrap: wrap;
}
.stock-name {
  font-weight: 600;
  font-size: 1.05rem;
}
.stock-code {
  color: var(--text-dim);
  font-size: 0.85rem;
}
.scores {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
  flex-shrink: 0;
  cursor: help;
}
.scores .dyn {
  color: var(--brand, #409eff);
  font-weight: 700;
  font-size: 0.95rem;
}
.scores .pool {
  color: var(--text-dim);
  font-size: 0.72rem;
}
.field-line {
  font-size: 0.85rem;
  line-height: 1.6;
  margin-bottom: 6px;
}
.field-line.reason {
  margin-bottom: 10px;
}
.field-label {
  color: var(--text-dim);
}
.factors {
  display: flex;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 0.85rem;
  margin-bottom: 8px;
}
.factor-tag {
  margin: 0;
}
.report-collapse {
  margin-top: 8px;
}
.report-text {
  font-size: 0.85rem;
  line-height: 1.6;
  color: var(--text-dim);
  white-space: pre-wrap;
}
</style>
