<template>
  <el-dialog v-model="visible" title="新闻影响力详情" width="720px" destroy-on-close>
    <div v-if="news" class="detail-content">
      <h3>{{ news.title }}</h3>
      <div class="meta-row">
        <span class="type-tag" :class="`t-${news.event_type || 'news'}`">
          {{ typeLabel(news.event_type) }}
        </span>
        <span class="bias" :class="dirClass(news.bull_bear_bias)">
          {{ biasLabel }}
        </span>
        <el-tag size="small" type="info">{{ news.source }}</el-tag>
        <span v-if="news.source_count > 1" class="src-count">
          {{ news.source_count }} 家报道
        </span>
        <span class="time">{{ news.pub_time }}</span>
      </div>

      <div class="metric-grid">
        <div class="metric-cell">
          <div class="metric-num" :class="{ key: news.importance >= 8 }">
            {{ fmtNum(news.importance, 1) }}
          </div>
          <div class="metric-name">重要度</div>
        </div>
        <div class="metric-cell">
          <div class="metric-num">{{ biasLabel }}</div>
          <div class="metric-name">多空判断</div>
        </div>
        <div class="metric-cell">
          <div class="metric-num">{{ confLabel(news.confidence) }}</div>
          <div class="metric-name">置信度 {{ fmtNum(news.confidence, 2) }}</div>
        </div>
        <div class="metric-cell">
          <div class="metric-num">{{ news.horizon || '—' }}</div>
          <div class="metric-name">影响窗口</div>
        </div>
      </div>

      <div v-if="impacts.length" class="section">
        <h4>影响链（细分赛道 · 方向 · 预期幅度）</h4>
        <el-table :data="impacts" size="small" stripe>
          <el-table-column prop="name" label="细分赛道" min-width="150" />
          <el-table-column label="方向" width="86" align="center">
            <template #default="{ row }">
              <span class="bias" :class="dirClass(row.direction)">
                {{ dirArrow(row.direction) }} {{ strengthLabel(row.direction, row.strength) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="弹性" width="70" align="center">
            <template #default="{ row }">{{ fmtNum(row.elasticity, 2) }}</template>
          </el-table-column>
          <el-table-column label="预期区间" width="150" align="center">
            <template #default="{ row }">
              <span class="gain" :class="dirClass(row.direction)">
                {{ fmtGain(row.exp_low) }}~{{ fmtGain(row.exp_high) }}%
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="basis" label="依据" min-width="170" show-overflow-tooltip />
        </el-table>
        <p class="hint">
          幅度不是模型猜的百分比：按「强度系数 × 该赛道近 20 日波动 σ × 弹性」校准，
          单日截断 ±8%、多日 ±15%。
        </p>
      </div>

      <div v-else class="section no-impact">
        <h4>影响行业</h4>
        <p>
          这条新闻<strong>没有落到任何细分赛道</strong>。它要么是宏观背景信息
          （商品价格、汇率利率、海外事件），要么对 A 股没有明确的传导链。
          按口径不给方向和幅度 —— 用「半导体」「家电」这类大行业硬套，
          只会让榜单看起来完整、实际不可交易。
        </p>
      </div>

      <div v-if="news.top_stocks?.length" class="section">
        <h4>弹性个股</h4>
        <el-table :data="news.top_stocks" size="small" stripe>
          <el-table-column prop="code" label="代码" width="90" />
          <el-table-column prop="name" label="名称" width="120" />
          <el-table-column label="弹性系数" width="100">
            <template #default="{ row }">{{ fmtNum(row.elasticity, 2) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <div v-if="news.debate_summary" class="section">
        <h4>判断依据</h4>
        <p>{{ news.debate_summary }}</p>
      </div>

      <div v-if="news.content" class="content-section">
        <h4>新闻内容</h4>
        <p class="news-body">{{ news.content }}</p>
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

const TYPE_LABELS = {
  policy: '政策',
  industry: '行业',
  company: '公司',
  capital: '资金',
  overseas: '海外',
}

const impacts = computed(() => props.news?.industry_impacts || [])

const biasLabel = computed(() => {
  const n = props.news
  if (!n) return '中性'
  return strengthLabel(n.bull_bear_bias, n.bias_strength, n.bias_scope)
})

function typeLabel(t) {
  return TYPE_LABELS[t] || '资讯'
}

function dirClass(direction) {
  if (direction === 'bullish') return 'up'
  if (direction === 'bearish') return 'down'
  return 'flat'
}

function dirArrow(direction) {
  if (direction === 'bullish') return '↑'
  if (direction === 'bearish') return '↓'
  return '→'
}

function strengthLabel(direction, strength, scope) {
  // 整体级方向（无 L3 主题）不给强弱措辞，避免读成"某个行业强多"
  if (scope === 'market') {
    if (direction === 'bullish') return '整体偏多'
    if (direction === 'bearish') return '整体偏空'
    return '中性'
  }
  if (direction === 'bullish') return ['', '弱多', '中多', '强多'][strength] || '偏多'
  if (direction === 'bearish') return ['', '弱空', '中空', '强空'][strength] || '偏空'
  return '中性'
}

function confLabel(c) {
  if (!c) return '—'
  if (c > 0.7) return '高'
  if (c >= 0.4) return '中'
  return '低'
}

function fmtNum(v, digits = 1) {
  if (v === undefined || v === null || Number.isNaN(Number(v))) return '—'
  return Number(v).toFixed(digits)
}

function fmtGain(v) {
  if (v === undefined || v === null) return '0.0'
  const n = Number(v)
  return `${n > 0 ? '+' : ''}${n.toFixed(1)}`
}
</script>

<style scoped>
.detail-content h3 {
  margin: 0 0 12px;
  font-size: 1.1rem;
  line-height: 1.4;
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
.src-count {
  font-size: 0.78rem;
  color: var(--brand, #409eff);
}
.type-tag {
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 0.72rem;
  border: 1px solid transparent;
}
.t-policy { color: #f56c6c; border-color: #f56c6c55; background: #f56c6c14; }
.t-industry { color: #e6a23c; border-color: #e6a23c55; background: #e6a23c14; }
.t-company { color: #409eff; border-color: #409eff55; background: #409eff14; }
.t-capital { color: #67c23a; border-color: #67c23a55; background: #67c23a14; }
.t-overseas { color: #9b7cf0; border-color: #9b7cf055; background: #9b7cf014; }
.bias {
  font-size: 0.8rem;
  padding: 1px 6px;
  border-radius: 3px;
  border: 1px solid transparent;
}
.up { color: #f56c6c; }
.bias.up { border-color: #f56c6c55; background: #f56c6c12; }
.down { color: #67c23a; }
.bias.down { border-color: #67c23a55; background: #67c23a12; }
.flat { color: var(--text-dim); }
.bias.flat { border-color: #88888844; }
.gain {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin: 16px 0;
}
.metric-cell {
  text-align: center;
}
.metric-num {
  font-size: 1.15rem;
  font-weight: 700;
  color: var(--brand, #409eff);
}
.metric-num.key {
  color: #f56c6c;
}
.metric-name {
  font-size: 0.78rem;
  color: var(--text-dim);
  margin-top: 4px;
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
.section .hint {
  font-size: 0.75rem;
  opacity: 0.75;
  margin-top: 6px;
}
/* 无主题说明：一条左侧虚线，避免读成"加载失败/数据缺失" */
.no-impact {
  padding: 10px 12px;
  border-left: 3px solid var(--border-color, rgba(128, 128, 128, 0.45));
  border-radius: 3px;
  background: var(--bg-dim, rgba(128, 128, 128, 0.06));
}
.no-impact h4 {
  margin-bottom: 6px;
}
.no-impact strong {
  color: var(--text);
}
.content-section {
  margin-top: 16px;
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
