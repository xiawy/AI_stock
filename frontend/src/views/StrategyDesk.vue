<template>
  <div>
    <AppHeader />
    <div class="page">
      <div class="page-header">
        <div>
          <h2>策略台 · 收盘研判</h2>
          <p class="sub">
            回答三个问题：今天涨的是哪一拨 · 这拨能不能持续 · 上一次判断错在哪
          </p>
        </div>
        <div class="header-actions">
          <el-date-picker
            v-model="selectedDate"
            type="date"
            placeholder="选择日期"
            format="YYYY-MM-DD"
            value-format="YYYY-MM-DD"
            @change="loadReport"
          />
          <el-button :loading="running" @click="runNow">立即研判</el-button>
          <el-button :loading="verifying" @click="verifyNow">回填断言</el-button>
        </div>
      </div>

      <el-alert
        v-if="report?.sections?.notice"
        :title="report.sections.notice"
        :type="isIntraday ? 'warning' : 'success'"
        :closable="false"
        class="notice"
      />

      <el-tabs v-model="tab" class="desk-tabs">
        <!-- ───────────────── 大盘总览 ───────────────── -->
        <el-tab-pane label="大盘总览" name="overview">
          <div v-loading="loading">
            <el-empty v-if="!hasReport" description="该日尚无策略台报告，可点击「立即研判」入队" :image-size="70" />

            <template v-else>
              <div class="overview-grid">
                <div v-for="item in sections.overview" :key="item.k" class="ov-card">
                  <div class="ov-k">{{ item.k }}</div>
                  <div class="ov-v">{{ item.v }}</div>
                </div>
              </div>

              <div v-if="sections.advice?.length" class="block">
                <h3>交易建议</h3>
                <ul class="bullet">
                  <li v-for="(a, i) in sections.advice" :key="i">{{ a }}</li>
                </ul>
              </div>

              <div v-if="sections.red_team" class="block red-team">
                <h3>红队反驳</h3>
                <p>{{ sections.red_team }}</p>
              </div>

              <div v-if="sections.risks?.length" class="block">
                <h3>风险</h3>
                <ul class="bullet warn">
                  <li v-for="(r, i) in sections.risks" :key="i">{{ r }}</li>
                </ul>
              </div>
            </template>

            <div v-if="sections.degraded?.length || sections.missing?.length" class="block">
              <h3>数据健康</h3>
              <div class="chips">
                <el-tag v-for="d in sections.degraded" :key="d" type="warning" effect="plain" size="small">
                  降级 · {{ d }}
                </el-tag>
                <el-tag v-for="m in sections.missing" :key="m" type="danger" effect="plain" size="small">
                  缺失 · {{ m }}
                </el-tag>
              </div>
              <p class="hint">
                降级不影响报告生成：缺一块数据仍会落库，否则记分体系会出现空洞。
              </p>
            </div>
          </div>
        </el-tab-pane>

        <!-- ───────────────── 关注板块 ───────────────── -->
        <el-tab-pane label="关注板块" name="boards">
          <div v-loading="loading">
            <el-empty v-if="!hasReport" description="该日尚无策略台报告" :image-size="70" />

            <template v-else>
              <div class="block">
                <h3>
                  板块结构拆解
                  <span class="count">{{ sections.boards?.length || 0 }} 个篮子</span>
                </h3>
                <el-table :data="sections.boards || []" size="small" class="desk-table">
                  <el-table-column prop="name" label="篮子" min-width="150" />
                  <el-table-column prop="parent" label="所属板块" width="110" />
                  <el-table-column prop="driver" label="驱动" width="88" />
                  <el-table-column prop="synth_pct" label="合成涨幅" width="96" align="right">
                    <template #default="{ row }">
                      <span :class="row.synth_pct >= 0 ? 'up' : 'down'">
                        {{ row.synth_pct >= 0 ? '+' : '' }}{{ row.synth_pct }}%
                      </span>
                    </template>
                  </el-table-column>
                  <el-table-column prop="heat" label="评级/有效样本" width="130" />
                  <el-table-column prop="scope" label="可交易范围" width="100">
                    <template #default="{ row }">
                      <el-tag :type="scopeType(row.scope)" effect="plain" size="small">
                        {{ row.scope }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column prop="calendar" label="催化窗口" min-width="150" />
                  <el-table-column prop="leaders" label="篮内高标" min-width="120" />
                  <el-table-column prop="basis" label="依据" min-width="240" show-overflow-tooltip />
                </el-table>
              </div>

              <div class="block">
                <h3>
                  交叉验证矩阵
                  <span class="count">新闻证据 {{ sections.news_hits ?? 0 }} 条</span>
                </h3>
                <el-table :data="sections.matrix || []" size="small" class="desk-table">
                  <el-table-column prop="basket_name" label="篮子" min-width="150" />
                  <el-table-column prop="sector_view" label="板块结构" width="130" />
                  <el-table-column prop="flow_view" label="资金" width="110" />
                  <el-table-column prop="news_view" label="新闻" width="110" />
                  <el-table-column prop="consistency" label="一致性" width="130">
                    <template #default="{ row }">
                      <span :class="consistencyClass(row.consistency)">{{ row.consistency }}</span>
                      <el-tag v-if="row.pseudo_resonance" type="warning" effect="plain" size="small" class="ml6">
                        伪共振
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column prop="note" label="说明" min-width="160" />
                </el-table>
                <p class="hint">
                  伪共振 = 三条证据其实引自同一事实（新闻只是把涨停归因复述一遍）。这类行不计入可交易信号。
                </p>
              </div>

              <div class="block">
                <h3>高标梯队溯源</h3>
                <el-table :data="sections.leaders || []" size="small" class="desk-table">
                  <el-table-column prop="name" label="名称" width="110" />
                  <el-table-column prop="symbol" label="代码" width="96" />
                  <el-table-column prop="board_type" label="连板" width="88" />
                  <el-table-column prop="driver" label="真实驱动" width="100" />
                  <el-table-column prop="attribution" label="归属" width="110" />
                  <el-table-column prop="fund" label="资金验证" width="110" />
                  <el-table-column prop="warn" label="外推提示" min-width="200" />
                </el-table>
                <el-empty v-if="!sections.leaders?.length" description="当日无 ≥3 连板高标" :image-size="50" />
              </div>
            </template>
          </div>
        </el-tab-pane>

        <!-- ───────────────── 记分 ───────────────── -->
        <el-tab-pane label="记分与回灌" name="score">
          <div v-loading="scoreLoading">
            <div class="stat-row">
              <div class="stat">
                <div class="stat-v">{{ score.total ?? 0 }}</div>
                <div class="stat-k">断言总数</div>
              </div>
              <div class="stat">
                <div class="stat-v">{{ score.judged ?? 0 }}</div>
                <div class="stat-k">已判定</div>
              </div>
              <div class="stat">
                <div class="stat-v">{{ pct(score.overall?.win_rate) }}</div>
                <div class="stat-k">兑现率</div>
              </div>
              <div class="stat">
                <div class="stat-v">{{ pct(score.unjudged_rate) }}</div>
                <div class="stat-k">无法判定率</div>
              </div>
            </div>

            <div class="block">
              <h3>按驱动源分组</h3>
              <el-table :data="driverRows" size="small" class="desk-table">
                <el-table-column prop="driver" label="驱动" width="110" />
                <el-table-column prop="n" label="总数" width="90" align="right" />
                <el-table-column prop="judged" label="已判定" width="90" align="right" />
                <el-table-column prop="unjudged_rate" label="无法判定率" width="120" align="right">
                  <template #default="{ row }">{{ pct(row.unjudged_rate) }}</template>
                </el-table-column>
                <el-table-column prop="win_rate" label="兑现率" width="100" align="right">
                  <template #default="{ row }">{{ pct(row.win_rate) }}</template>
                </el-table-column>
                <el-table-column prop="coef" label="权重系数" width="110" align="right">
                  <template #default="{ row }">
                    <span v-if="row.coef">{{ row.coef.toFixed(3) }}</span>
                    <el-tooltip v-else :content="coefSkip(row.driver)" placement="top">
                      <span class="dim">未出系数</span>
                    </el-tooltip>
                  </template>
                </el-table-column>
              </el-table>
              <el-empty v-if="!driverRows.length" description="尚无已判定断言" :image-size="50" />
            </div>

            <div class="block coef-panel">
              <h3>
                权重回灌
                <el-tag :type="coef.enabled ? 'success' : 'info'" size="small" effect="plain">
                  {{ coef.enabled ? '已开启' : '观察中（未生效）' }}
                </el-tag>
              </h3>
              <p class="hint">
                系数按 driver 胜率线性映射到 0.8~1.2，<b>只作优先级乘子</b>，
                不参与任何硬门槛。周频重算，样本不足 30 条或无法判定率超 30% 的 driver 不出系数。
              </p>
              <div class="coef-meta">
                <span>最近重算：{{ coef.updated_at || '—' }}</span>
                <span>基准日：{{ coef.as_of || '—' }}</span>
                <el-button size="small" :loading="coefLoading" @click="recompute">立即重算</el-button>
              </div>
              <div v-if="skipList.length" class="chips">
                <el-tag v-for="s in skipList" :key="s.driver" type="info" effect="plain" size="small">
                  {{ s.driver }}：{{ s.reason }}
                </el-tag>
              </div>
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>
    <DisclaimerFooter />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import AppHeader from '../components/AppHeader.vue'
import DisclaimerFooter from '../components/DisclaimerFooter.vue'
import { strategyApi } from '../api/strategy'

const tab = ref('overview')
const selectedDate = ref('')
const loading = ref(false)
const running = ref(false)
const verifying = ref(false)
const scoreLoading = ref(false)
const coefLoading = ref(false)

const report = ref(null)
const score = ref({})
const coef = ref({ coefs: {}, skipped: {}, enabled: false })

const sections = computed(() => report.value?.sections || {})
const hasReport = computed(() => !!report.value && !report.value.empty)
const isIntraday = computed(() => report.value?.snapshot_type === 'intraday')

/** 驱动行 = 质量表 ∪ 系数表，保证"有系数但没样本"这种脏数据也能看见 */
const driverRows = computed(() => {
  const q = score.value.driver_quality || {}
  const byDriver = score.value.by_driver || {}
  const names = new Set([...Object.keys(q), ...Object.keys(byDriver), ...Object.keys(coef.value.coefs || {})])
  return [...names].sort().map((driver) => ({
    driver,
    n: q[driver]?.n ?? byDriver[driver]?.n ?? 0,
    judged: q[driver]?.judged ?? byDriver[driver]?.n ?? 0,
    unjudged_rate: q[driver]?.unjudged_rate ?? 0,
    win_rate: q[driver]?.win_rate ?? byDriver[driver]?.win_rate ?? null,
    coef: coef.value.coefs?.[driver] ?? null,
  }))
})

const skipList = computed(() =>
  Object.entries(coef.value.skipped || {}).map(([driver, reason]) => ({ driver, reason })),
)

function coefSkip(driver) {
  return coef.value.skipped?.[driver] || '无已判定样本'
}

function pct(v) {
  if (v === null || v === undefined) return '—'
  return `${(Number(v) * 100).toFixed(1)}%`
}

function scopeType(scope) {
  if (scope === '板块级') return 'success'
  if (scope === '观察') return 'warning'
  return 'info'
}

function consistencyClass(text) {
  if (!text) return 'dim'
  if (text.startsWith('✅')) return 'up'
  if (text.startsWith('❌')) return 'down'
  if (text.startsWith('⚠️')) return 'dim-warn'
  return 'dim'
}

async function loadReport() {
  loading.value = true
  try {
    report.value = await strategyApi.desk(selectedDate.value || undefined)
  } catch (e) {
    report.value = null
    ElMessage.error(e?.response?.data?.detail || '读取策略台报告失败')
  } finally {
    loading.value = false
  }
}

async function loadScore() {
  scoreLoading.value = true
  try {
    score.value = await strategyApi.scoreboard()
    coef.value = await strategyApi.coef()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '读取记分看板失败')
  } finally {
    scoreLoading.value = false
  }
}

async function runNow() {
  running.value = true
  try {
    const res = await strategyApi.run(selectedDate.value || undefined)
    ElMessage.success(`已入队：${res.task_id}（收盘后生成，刷新可查看）`)
  } catch (e) {
    ElMessage.warning(e?.response?.data?.detail || '入队失败')
  } finally {
    running.value = false
  }
}

async function verifyNow() {
  verifying.value = true
  try {
    const res = await strategyApi.verify()
    ElMessage.success(`回填完成：判定 ${res.verified} 条，跳过 ${res.skipped} 条`)
    await loadScore()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '回填失败')
  } finally {
    verifying.value = false
  }
}

async function recompute() {
  coefLoading.value = true
  try {
    const res = await strategyApi.recomputeCoef()
    coef.value = await strategyApi.coef()
    ElMessage.success(`已重算：${Object.keys(res.coefs || {}).length} 个 driver 出系数`)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '重算失败')
  } finally {
    coefLoading.value = false
  }
}

watch(tab, (val) => {
  if (val === 'score' && !score.value.total) loadScore()
})

onMounted(() => {
  loadReport()
  loadScore()
})
</script>

<style scoped>
.page {
  max-width: 1200px;
  margin: 0 auto;
  padding: 24px;
}
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.page-header h2 {
  margin: 0 0 6px;
}
.sub {
  margin: 0;
  color: var(--text-dim);
  font-size: 0.85rem;
}
.header-actions {
  display: flex;
  gap: 10px;
  align-items: center;
}
.notice {
  margin-bottom: 12px;
}
.desk-tabs {
  margin-top: 4px;
}
.overview-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
  margin-bottom: 20px;
}
.ov-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
}
.ov-k {
  color: var(--text-dim);
  font-size: 0.75rem;
  letter-spacing: 0.5px;
  margin-bottom: 8px;
}
.ov-v {
  color: var(--text);
  font-size: 0.92rem;
  line-height: 1.5;
  word-break: break-word;
}
.block {
  margin-bottom: 26px;
}
.block h3 {
  font-size: 1rem;
  margin: 0 0 10px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.count {
  color: var(--text-dim);
  font-size: 0.78rem;
  font-weight: 400;
}
.bullet {
  margin: 0;
  padding-left: 20px;
  line-height: 1.8;
}
.bullet.warn li {
  color: #ffb27a;
}
.red-team {
  border-left: 3px solid var(--brand);
  padding-left: 14px;
  color: var(--text-dim);
}
.red-team p {
  margin: 0;
  line-height: 1.7;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.hint {
  color: var(--text-dim);
  font-size: 0.78rem;
  margin: 8px 0 0;
  line-height: 1.6;
}
.desk-table {
  background: transparent;
}
.up {
  color: #ff5a5a;
}
.down {
  color: #35c46a;
}
.dim {
  color: var(--text-dim);
}
.dim-warn {
  color: #e6b955;
}
.ml6 {
  margin-left: 6px;
}
.stat-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 22px;
}
.stat {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
  text-align: center;
}
.stat-v {
  font-size: 1.5rem;
  font-weight: 600;
  color: var(--brand);
}
.stat-k {
  color: var(--text-dim);
  font-size: 0.78rem;
  margin-top: 6px;
}
.coef-panel {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
  background: var(--bg-panel);
}
.coef-meta {
  display: flex;
  align-items: center;
  gap: 18px;
  color: var(--text-dim);
  font-size: 0.82rem;
  margin: 10px 0;
}
</style>
