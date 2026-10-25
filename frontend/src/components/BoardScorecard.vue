<template>
  <el-dialog v-model="visible" title="三榜成绩单" width="820px" top="6vh">
    <div v-loading="loading" class="scorecard">
      <p class="lead">
        每条判断都能点开"上次这么说，结果如何"。三条纪律：
        <b>分母只算已判定样本</b>、<b>样本 &lt; {{ minSamples }} 不出胜率</b>、
        <b>只读不回写阈值</b>（一旦自动调参，就会重演"单一日频信号驱动全局参数"的死锁）。
      </p>

      <el-alert
        v-if="error"
        type="warning"
        :closable="false"
        show-icon
        :title="error"
        class="notes"
      />

      <!-- 新闻榜 -->
      <section class="block">
        <h4>新闻榜 · 方向与幅度</h4>
        <p class="metric">{{ data.news?.metric || '主题级方向命中率（成分股等权）' }}</p>
        <div class="kv">
          <span>已判定 <b>{{ data.news?.judged ?? 0 }}</b> 条</span>
          <span>待验证 <b>{{ data.news?.pending ?? 0 }}</b> 条</span>
          <span>
            方向命中率
            <b :class="rateClass(data.news?.overall?.direction_hit_rate, data.news?.overall?.sample_ok)">
              {{ rateText(data.news?.overall?.direction_hit_rate, data.news?.overall?.sample_ok) }}
            </b>
          </span>
          <span>
            区间命中率
            <b :class="rateClass(data.news?.overall?.range_hit_rate, data.news?.overall?.sample_ok)">
              {{ rateText(data.news?.overall?.range_hit_rate, data.news?.overall?.sample_ok) }}
            </b>
          </span>
        </div>
        <el-table
          v-if="newsTypeRows.length"
          :data="newsTypeRows"
          size="small"
          class="mini-table"
        >
          <el-table-column prop="label" label="事件类型" width="120" />
          <el-table-column prop="n" label="样本" width="80" align="right" />
          <el-table-column label="方向命中" width="110" align="right">
            <template #default="{ row }">
              {{ rateText(row.direction_hit_rate, row.sample_ok) }}
            </template>
          </el-table-column>
          <el-table-column label="区间命中" width="110" align="right">
            <template #default="{ row }">
              {{ rateText(row.range_hit_rate, row.sample_ok) }}
            </template>
          </el-table-column>
        </el-table>
        <p v-else-if="!data.news?.ok" class="dim">{{ data.news?.reason || '暂无回测台账' }}</p>
      </section>

      <!-- 行业榜 -->
      <section class="block">
        <h4>行业榜 · 排序有效性</h4>
        <p class="metric">{{ data.industry?.metric || 'Top5 的 5 日累计涨跌幅 vs 全行业等权' }}</p>
        <div class="kv">
          <span>样本 <b>{{ data.industry?.n ?? 0 }}</b> 个（行业×期次）</span>
          <span>
            跑赢比例
            <b :class="rateClass(data.industry?.hit_rate, data.industry?.sample_ok)">
              {{ rateText(data.industry?.hit_rate, data.industry?.sample_ok) }}
            </b>
          </span>
          <span>平均超额 <b>{{ pct(data.industry?.avg_excess_pct) }}</b></span>
          <span>最优 <b class="up">{{ pct(data.industry?.best_excess_pct) }}</b></span>
          <span>最差 <b class="down">{{ pct(data.industry?.worst_excess_pct) }}</b></span>
        </div>
        <p v-if="!data.industry?.ok" class="dim">{{ data.industry?.reason }}</p>
      </section>

      <!-- 热股榜 -->
      <section class="block">
        <h4>热股榜 · 入池后表现</h4>
        <p class="metric">{{ data.pool?.metric || '入池 5 日存活率 / 鱼尾率' }}</p>
        <div class="kv">
          <span>样本 <b>{{ data.pool?.n ?? 0 }}</b> 条</span>
          <span>
            5 日存活率
            <b :class="rateClass(data.pool?.survival_rate, data.pool?.sample_ok)">
              {{ rateText(data.pool?.survival_rate, data.pool?.sample_ok) }}
            </b>
          </span>
          <span>
            鱼尾标记率
            <b>{{ rateText(data.pool?.tail_rate, data.pool?.sample_ok) }}</b>
          </span>
          <span>平均动态分 <b>{{ data.pool?.avg_dyn ?? '—' }}</b></span>
        </div>

        <p class="reverse" :class="{ warn: isLowBought }">
          <b>反向指标 · 榜首 5 条被买入比例：</b>
          {{ rateText(data.pool?.top_bought?.ratio, data.pool?.top_bought?.sample_ok) }}
          <span class="dim">（{{ data.pool?.top_bought?.note || '长期接近 0 说明榜单与交易脱节' }}）</span>
        </p>

        <div v-if="data.pool?.returns && !data.pool.returns.ok" class="returns-off">
          <span class="dim">{{ data.pool.returns.reason }}</span>
          <el-button size="small" type="primary" plain :loading="loadingReturns" @click="loadReturns">
            计算 5 日收益
          </el-button>
        </div>
        <div v-else-if="data.pool?.returns?.ok" class="kv">
          <span>收益样本 <b>{{ data.pool.returns.n }}</b></span>
          <span>胜率 <b>{{ rateText(data.pool.returns.win_rate, data.pool.returns.sample_ok) }}</b></span>
          <span>平均收益 <b>{{ pct(data.pool.returns.avg_return) }}</b></span>
        </div>
      </section>
    </div>
  </el-dialog>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { boardApi } from '../api/board'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  days: { type: Number, default: 30 },
})
const emit = defineEmits(['update:modelValue'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const loading = ref(false)
const loadingReturns = ref(false)
const error = ref('')
const data = ref({})
const minSamples = computed(() => data.value?.min_samples ?? 30)

const newsTypeRows = computed(() => {
  const rows = data.value?.news?.by_event_type || {}
  const label = { policy: '政策', industry: '行业', company: '公司', capital: '资金', overseas: '海外' }
  return Object.entries(rows).map(([k, v]) => ({
    label: label[k] || k,
    ...v,
  }))
})

const isLowBought = computed(() => {
  const tb = data.value?.pool?.top_bought
  return tb?.sample_ok && tb.ratio != null && tb.ratio < 0.1
})

async function load(includeReturns = false) {
  if (includeReturns) loadingReturns.value = true
  else loading.value = true
  error.value = ''
  try {
    const { data: payload } = await boardApi.scorecard({
      days: props.days,
      includeReturns,
    })
    data.value = payload
  } catch {
    error.value = '成绩单加载失败，请稍后重试'
    ElMessage.error(error.value)
  } finally {
    loading.value = false
    loadingReturns.value = false
  }
}

function loadReturns() {
  load(true)
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) load(false)
  },
)

/** 样本不足时不给数字 —— 这是纪律，不是缺陷 */
function rateText(rate, sampleOk) {
  if (rate == null) return '—'
  if (sampleOk === false) return '样本不足'
  return `${(rate * 100).toFixed(1)}%`
}

function rateClass(rate, sampleOk) {
  if (rate == null || sampleOk === false) return 'dim'
  return rate >= 0.5 ? 'up' : 'down'
}

function pct(v) {
  if (v == null) return '—'
  return `${v >= 0 ? '+' : ''}${Number(v).toFixed(2)}%`
}
</script>

<style scoped>
.scorecard {
  max-height: 72vh;
  overflow-y: auto;
  padding-right: 6px;
}
.lead {
  margin: 0 0 14px;
  color: var(--text-dim);
  font-size: 0.8rem;
  line-height: 1.65;
}
.notes {
  margin-bottom: 12px;
}
.block {
  margin-bottom: 20px;
  padding-bottom: 14px;
  border-bottom: 1px dashed var(--border);
}
.block:last-child {
  border-bottom: none;
}
.block h4 {
  margin: 0 0 4px;
  font-size: 0.94rem;
  padding-left: 8px;
  border-left: 3px solid var(--brand, #409eff);
}
.metric {
  margin: 0 0 10px;
  font-size: 0.75rem;
  color: var(--text-dim);
  line-height: 1.5;
}
.kv {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  font-size: 0.82rem;
  color: var(--text-dim);
}
.kv b {
  color: var(--text-main, inherit);
}
.mini-table {
  margin-top: 10px;
}
.reverse {
  margin: 12px 0 0;
  font-size: 0.8rem;
  line-height: 1.6;
}
.reverse.warn {
  color: #e6a23c;
}
.returns-off {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 10px;
  font-size: 0.78rem;
}
.up {
  color: #f56c6c;
}
.down {
  color: #67c23a;
}
.dim {
  color: var(--text-dim);
  font-size: 0.78rem;
}
</style>
