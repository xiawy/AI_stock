<template>
  <el-card class="stock-card" :class="{ alternate: stock.is_alternate }" shadow="hover">
    <div class="card-header">
      <div class="rank-badge">#{{ stock.rank }}</div>
      <div class="stock-info">
        <span class="stock-name">{{ stock.stock_name }}</span>
        <span class="stock-code">{{ stock.ticker }}</span>
        <el-tag size="small" :type="riskTagType">{{ stock.risk_level || '中' }}风险</el-tag>
        <el-tag size="small" type="info">{{ stock.holding_period || '短线' }}</el-tag>
      </div>
      <div v-if="stock.is_alternate" class="alt-badge">备选</div>
    </div>

    <div class="card-body">
      <div class="score-row">
        <span class="score-label">综合</span>
        <span class="score-value primary">{{ stock.final_score?.toFixed(1) }}</span>
        <span class="score-label">基本面</span>
        <span class="score-value">{{ stock.fundamentals_score?.toFixed(0) }}</span>
        <span class="score-label">技术面</span>
        <span class="score-value">{{ stock.technical_score?.toFixed(0) }}</span>
        <span class="score-label">事件</span>
        <span class="score-value">{{ stock.event_match_score?.toFixed(0) }}</span>
        <span class="score-label">辩论</span>
        <span class="score-value">{{ stock.debate_score?.toFixed(0) }}</span>
      </div>

      <div class="industry">{{ stock.industry }}</div>

      <div class="trigger" v-if="stock.trigger_event">
        <span class="trigger-label">触发事件：</span>{{ stock.trigger_event }}
      </div>

      <div class="buy-logic" v-if="stock.buy_logic">
        <span class="logic-label">买入逻辑：</span>{{ stock.buy_logic }}
      </div>

      <div class="price-row">
        <div class="price-item" v-if="stock.target_price > 0">
          <span class="price-label">目标价</span>
          <span class="price-value">{{ stock.target_price?.toFixed(2) }}</span>
        </div>
        <div class="price-item" v-if="stock.expected_gain_high > 0">
          <span class="price-label">预期涨幅</span>
          <span class="price-value gain">
            {{ stock.expected_gain_low?.toFixed(1) }}% ~ {{ stock.expected_gain_high?.toFixed(1) }}%
          </span>
        </div>
        <div class="price-item" v-if="stock.stop_loss_price > 0">
          <span class="price-label">止损价</span>
          <span class="price-value loss">{{ stock.stop_loss_price?.toFixed(2) }}</span>
        </div>
      </div>

      <el-collapse v-if="stock.bull_bear_summary" class="debate-collapse">
        <el-collapse-item title="多空辩论摘要">
          <p class="debate-text">{{ stock.bull_bear_summary }}</p>
        </el-collapse-item>
      </el-collapse>
    </div>
  </el-card>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  stock: { type: Object, required: true },
})

const riskTagType = computed(() => {
  const r = props.stock.risk_level
  if (r === '高') return 'danger'
  if (r === '低') return 'success'
  return 'warning'
})
</script>

<style scoped>
.stock-card {
  margin-bottom: 16px;
  border-left: 3px solid var(--brand, #409eff);
}
.stock-card.alternate {
  border-left-color: #909399;
  opacity: 0.85;
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
}
.stock-info {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
}
.stock-name {
  font-weight: 600;
  font-size: 1.05rem;
}
.stock-code {
  color: var(--text-dim);
  font-size: 0.85rem;
}
.alt-badge {
  background: #909399;
  color: #fff;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 0.75rem;
}
.score-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.score-label {
  color: var(--text-dim);
  font-size: 0.8rem;
}
.score-value {
  font-weight: 600;
  font-size: 0.9rem;
  margin-right: 8px;
}
.score-value.primary {
  color: var(--brand, #409eff);
  font-size: 1.1rem;
}
.industry {
  color: var(--text-dim);
  font-size: 0.85rem;
  margin-bottom: 6px;
}
.trigger {
  font-size: 0.85rem;
  margin-bottom: 6px;
}
.trigger-label {
  color: var(--text-dim);
}
.buy-logic {
  font-size: 0.85rem;
  margin-bottom: 10px;
  line-height: 1.5;
}
.logic-label {
  color: var(--text-dim);
}
.price-row {
  display: flex;
  gap: 20px;
  margin-bottom: 10px;
}
.price-item {
  display: flex;
  flex-direction: column;
  align-items: center;
}
.price-label {
  font-size: 0.75rem;
  color: var(--text-dim);
}
.price-value {
  font-weight: 600;
}
.price-value.gain {
  color: #f56c6c;
}
.price-value.loss {
  color: #67c23a;
}
.debate-collapse {
  margin-top: 8px;
}
.debate-text {
  font-size: 0.85rem;
  line-height: 1.6;
  color: var(--text-dim);
}
</style>
