<template>
  <el-card class="stock-card" shadow="hover">
    <div class="card-header">
      <div class="rank-badge">#{{ rank }}</div>
      <div class="stock-info">
        <span class="stock-name">{{ stock.name }}</span>
        <span class="stock-code">{{ stock.symbol }}</span>
        <el-tag v-if="stock.industry" size="small" type="info">{{ stock.industry }}</el-tag>
        <el-tag size="small" :type="statusType">{{ statusLabel }}</el-tag>
      </div>
      <span class="confidence">置信 {{ confidenceText(stock.confidence) }}</span>
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
.confidence {
  color: var(--brand, #409eff);
  font-weight: 700;
  font-size: 0.95rem;
  flex-shrink: 0;
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
