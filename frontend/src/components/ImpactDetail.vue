<template>
  <el-dialog v-model="visible" title="新闻影响力详情" width="680px" destroy-on-close>
    <div v-if="news" class="detail-content">
      <h3>{{ news.title }}</h3>
      <div class="meta-row">
        <el-tag size="small">{{ news.source }}</el-tag>
        <el-tag size="small" :type="news.category === 'policy' ? 'danger' : 'info'">
          {{ news.category === 'policy' ? '政策' : '资讯' }}
        </el-tag>
        <el-tag size="small" :type="biasType">{{ biasLabel }}</el-tag>
        <span class="time">{{ news.pub_time }}</span>
      </div>

      <el-divider />

      <div class="scores-grid">
        <div class="score-cell">
          <div class="score-num">{{ news.policy_score?.toFixed(1) }}</div>
          <div class="score-name">政策影响力</div>
        </div>
        <div class="score-cell">
          <div class="score-num">{{ news.news_score?.toFixed(1) }}</div>
          <div class="score-name">新闻重要性</div>
        </div>
        <div class="score-cell">
          <div class="score-num">{{ news.capital_score?.toFixed(1) }}</div>
          <div class="score-name">游资吸引力</div>
        </div>
        <div class="score-cell">
          <div class="score-num">{{ news.sentiment_score?.toFixed(1) }}</div>
          <div class="score-name">舆情影响力</div>
        </div>
      </div>

      <div class="composite">
        综合评分：<strong>{{ news.composite_score?.toFixed(2) }}</strong>
      </div>

      <div v-if="news.content" class="content-section">
        <h4>新闻内容</h4>
        <p class="news-body">{{ news.content }}</p>
      </div>

      <div v-if="news.industries?.length" class="section">
        <h4>影响行业</h4>
        <div class="tag-list">
          <el-tag v-for="ind in news.industries" :key="ind" size="small">{{ ind }}</el-tag>
        </div>
      </div>

      <div v-if="news.top_stocks?.length" class="section">
        <h4>弹性个股</h4>
        <el-table :data="news.top_stocks" size="small" stripe>
          <el-table-column prop="code" label="代码" width="80" />
          <el-table-column prop="name" label="名称" width="100" />
          <el-table-column prop="elasticity" label="弹性系数" width="100">
            <template #default="{ row }">{{ row.elasticity?.toFixed(2) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <div v-if="news.supply_demand && Object.keys(news.supply_demand).length" class="section">
        <h4>供需分析</h4>
        <p>缺口类型：{{ news.supply_demand.gap_type }}</p>
        <p>弹性系数：{{ news.supply_demand.elasticity_coefficient }}</p>
        <p v-if="news.supply_demand.expected_gain_high > 0">
          预期涨幅：{{ news.supply_demand.expected_gain_low?.toFixed(1) }}% ~
          {{ news.supply_demand.expected_gain_high?.toFixed(1) }}%
        </p>
      </div>

      <div v-if="news.debate_summary" class="section">
        <h4>多空辩论</h4>
        <p>{{ news.debate_summary }}</p>
      </div>
    </div>
  </el-dialog>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  modelValue: Boolean,
  news: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const biasType = computed(() => {
  const b = props.news?.bull_bear_bias
  if (b === 'bullish') return 'success'
  if (b === 'bearish') return 'danger'
  return 'info'
})

const biasLabel = computed(() => {
  const b = props.news?.bull_bear_bias
  if (b === 'bullish') return '偏多'
  if (b === 'bearish') return '偏空'
  return '中性'
})
</script>

<style scoped>
.detail-content h3 {
  margin: 0 0 12px;
  font-size: 1.1rem;
}
.meta-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.time {
  color: var(--text-dim);
  font-size: 0.85rem;
}
.scores-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin: 16px 0;
}
.score-cell {
  text-align: center;
}
.score-num {
  font-size: 1.3rem;
  font-weight: 700;
  color: var(--brand, #409eff);
}
.score-name {
  font-size: 0.8rem;
  color: var(--text-dim);
  margin-top: 4px;
}
.composite {
  text-align: center;
  font-size: 1rem;
  margin-bottom: 16px;
}
.section {
  margin-top: 16px;
}
.section h4 {
  font-size: 0.95rem;
  margin-bottom: 8px;
  color: var(--text);
}
.section p {
  font-size: 0.85rem;
  line-height: 1.6;
  color: var(--text-dim);
}
.tag-list {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.content-section {
  margin-top: 12px;
}
.content-section h4 {
  font-size: 0.95rem;
  margin-bottom: 6px;
}
.content-section p {
  font-size: 0.85rem;
  line-height: 1.6;
  color: var(--text-dim);
}
/* Full news body: preserve paragraph breaks, cap height with scroll. */
.news-body {
  white-space: pre-wrap;
  max-height: 260px;
  overflow-y: auto;
}
</style>
