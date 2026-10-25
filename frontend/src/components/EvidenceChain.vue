<template>
  <el-dialog
    v-model="visible"
    :title="`证据链 · ${theme || '—'}`"
    width="880px"
    top="6vh"
  >
    <div v-loading="loading" class="chain">
      <p class="lead">
        同一个主题在三个榜上的全部证据。<b>四段可点</b>：新闻是事实，行业是结构，
        龙头是标的，池内是系统当前的选择。
      </p>

      <el-alert
        v-if="notes.length"
        type="info"
        :closable="false"
        show-icon
        class="notes"
      >
        <p v-for="(n, i) in notes" :key="i" class="note-line">{{ n }}</p>
      </el-alert>

      <!-- 1 新闻 -->
      <div class="seg">
        <h4>① 新闻（事实层）<em>{{ news.length }} 条</em></h4>
        <div v-if="news.length" class="news-list">
          <div v-for="n in news" :key="n.id" class="news-item" @click="openDetail(n)">
            <div class="news-head">
              <span class="type-tag">{{ typeLabel(n.event_type) }}</span>
              <span class="title">{{ n.title }}</span>
              <span v-if="n.source_count > 1" class="multi" :title="`${n.source_count} 家媒体报道同一事件`">
                ×{{ n.source_count }}
              </span>
            </div>
            <div class="meta">
              <span v-if="n.source">{{ n.source }}</span>
              <span v-if="n.pub_time">{{ n.pub_time }}</span>
              <span class="src-badge" :class="n.assess_source === 'llm' ? 'llm' : 'rule'">
                {{ n.assess_source === 'llm' ? '模型评估' : '规则兜底' }}
              </span>
              <span
                v-for="i in n.impacts.filter((x) => isTheme(x.name))"
                :key="i.name"
                class="impact-chip"
                :class="dirClass(i.direction)"
              >
                {{ i.name }} {{ dirArrow(i.direction) }}
              </span>
            </div>
          </div>
        </div>
        <p v-else class="dim">该主题在当前快照里没有落主题的新闻。</p>
      </div>

      <!-- 2 行业 -->
      <div class="seg">
        <h4>② 行业（结构层）</h4>
        <div v-if="industry" class="ind-row">
          <span class="ind-name">{{ industry.industry }}</span>
          <span class="dim">行业榜第 {{ industry.rank }} 名</span>
          <span class="dim">优选级 {{ fmt(industry.heat_score) }}</span>
          <span v-if="industry.stage" class="chip">{{ industry.stage }}</span>
          <span :class="pctClass(industry.change_pct)">{{ pctText(industry.change_pct) }}</span>
          <span :class="pctClass(industry.main_net_inflow)" class="dim">
            主力 {{ flowText(industry.main_net_inflow) }}
          </span>
        </div>
        <p v-else class="dim">行业榜没有该主题（或名称差异过大）。</p>
      </div>

      <!-- 3 龙头 -->
      <div class="seg">
        <h4>③ 龙头（标地层）<em>{{ leaders.length }} 只</em></h4>
        <div v-if="leaders.length" class="leader-list">
          <div v-for="s in leaders" :key="s.code" class="leader-row">
            <span class="name">{{ s.name }}</span>
            <span class="code">{{ s.code }}</span>
            <el-tag
              v-for="lb in s.labels || (s.leader_label ? [s.leader_label] : [])"
              :key="lb"
              size="small"
              effect="plain"
              :type="lb === '领涨' ? 'danger' : 'warning'"
            >
              {{ lb }}
            </el-tag>
            <span :class="pctClass(s.change_pct)">{{ pctText(s.change_pct) }}</span>
            <span class="dim">换手 {{ s.turnover_rate != null ? s.turnover_rate.toFixed(1) + '%' : '—' }}</span>
          </div>
        </div>
        <p v-else class="dim">没有该主题的龙头股数据。</p>
      </div>

      <!-- 4 池内 -->
      <div class="seg">
        <h4>④ 自选池（系统当前选择）<em>{{ pool.length }} 只</em></h4>
        <div v-if="pool.length" class="leader-list">
          <div v-for="s in pool" :key="s.symbol" class="leader-row">
            <span class="name">{{ s.name }}</span>
            <span class="code">{{ s.symbol }}</span>
            <el-tag v-if="s.dyn_state && s.dyn_state !== '正常'" size="small" effect="plain"
                    :type="s.dyn_state === '增强' ? 'danger' : 'info'">
              动态{{ s.dyn_state }}
            </el-tag>
            <el-tag v-if="s.is_tail" size="small" type="warning" effect="plain">鱼尾</el-tag>
            <span class="dim">动态 {{ fmt(s.dyn_confidence) }}</span>
            <span class="dim">入池分 {{ fmt(s.confidence) }}</span>
          </div>
        </div>
        <p v-else class="dim">自选池没有该主题的标的。</p>
      </div>
    </div>

    <ImpactDetail v-model="detailVisible" :news="selectedNews" />
  </el-dialog>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { boardApi } from '../api/board'
import ImpactDetail from './ImpactDetail.vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  theme: { type: String, default: '' },
  date: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const loading = ref(false)
const data = ref(null)
const detailVisible = ref(false)
const selectedNews = ref(null)

const news = computed(() => data.value?.news || [])
const industry = computed(() => data.value?.industry || null)
const leaders = computed(() => data.value?.leaders || [])
const pool = computed(() => data.value?.pool || [])
const notes = computed(() => data.value?.notes || [])

async function load() {
  if (!props.theme) return
  loading.value = true
  try {
    const { data: payload } = await boardApi.chain(props.theme, props.date)
    data.value = payload
  } catch {
    data.value = null
    ElMessage.error('证据链加载失败，请稍后重试')
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.modelValue, props.theme],
  ([open]) => {
    if (open) load()
  },
  { immediate: true },
)

function isTheme(name) {
  return String(name || '').includes(props.theme) || props.theme.includes(String(name || ''))
}

function openDetail(n) {
  selectedNews.value = n
  detailVisible.value = true
}

function typeLabel(t) {
  return { policy: '政策', industry: '行业', company: '公司', capital: '资金', overseas: '海外' }[t] || '资讯'
}

function dirClass(d) {
  return d === 'bullish' ? 'up' : d === 'bearish' ? 'down' : 'dim'
}

function dirArrow(d) {
  return d === 'bullish' ? '↑' : d === 'bearish' ? '↓' : '→'
}

function pctText(v) {
  if (v == null) return '—'
  return `${v >= 0 ? '+' : ''}${Number(v).toFixed(2)}%`
}

function pctClass(v) {
  if (v == null) return 'dim'
  return v >= 0 ? 'up' : 'down'
}

function flowText(v) {
  if (v == null) return '—'
  const yi = v / 1e8
  return `${yi >= 0 ? '+' : ''}${yi.toFixed(2)}亿`
}

function fmt(v) {
  return v == null ? '—' : Number(v).toFixed(2)
}
</script>

<style scoped>
.chain {
  max-height: 70vh;
  overflow-y: auto;
  padding-right: 6px;
}
.lead {
  margin: 0 0 12px;
  color: var(--text-dim);
  font-size: 0.82rem;
  line-height: 1.6;
}
.notes {
  margin-bottom: 14px;
}
.note-line {
  margin: 2px 0;
  font-size: 0.78rem;
}
.seg {
  margin-bottom: 18px;
}
.seg h4 {
  margin: 0 0 8px;
  font-size: 0.92rem;
  padding-left: 8px;
  border-left: 3px solid var(--brand, #409eff);
}
.seg h4 em {
  font-style: normal;
  margin-left: 8px;
  font-weight: 400;
  font-size: 0.76rem;
  color: var(--text-dim);
}
.news-item {
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 8px;
  cursor: pointer;
}
.news-item:hover {
  border-color: var(--brand, #409eff);
}
.news-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.title {
  flex: 1;
  min-width: 0;
  font-size: 0.88rem;
  line-height: 1.5;
}
.type-tag {
  flex: none;
  padding: 0 5px;
  border-radius: 3px;
  font-size: 0.7rem;
  border: 1px solid var(--border);
  color: var(--text-dim);
}
.multi {
  flex: none;
  color: var(--brand, #409eff);
  font-size: 0.74rem;
}
.meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 5px;
  font-size: 0.75rem;
  color: var(--text-dim);
}
.src-badge {
  padding: 0 5px;
  border-radius: 3px;
  font-size: 0.68rem;
}
.src-badge.llm {
  color: #67c23a;
  border: 1px solid #67c23a55;
}
.src-badge.rule {
  color: #e6a23c;
  border: 1px solid #e6a23c55;
}
.impact-chip {
  padding: 0 5px;
  border-radius: 3px;
  border: 1px solid transparent;
}
.ind-row,
.leader-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 6px;
  font-size: 0.84rem;
}
.ind-name,
.leader-row .name {
  font-weight: 600;
}
.code {
  color: var(--text-dim);
  font-size: 0.76rem;
}
.chip {
  padding: 0 5px;
  border-radius: 3px;
  border: 1px solid var(--border);
  font-size: 0.72rem;
  color: var(--text-dim);
}
.up {
  color: #f56c6c;
  font-weight: 600;
}
.down {
  color: #67c23a;
  font-weight: 600;
}
.dim {
  color: var(--text-dim);
  font-size: 0.8rem;
}
</style>
