<template>
  <div class="report-viewer">
    <!-- Signal hero card -->
    <div class="signal-card">
      <div class="signal-caption">TRADING SIGNAL</div>
      <div class="signal-value" :style="{ color: style.color }">
        {{ (signal || 'N/A').toUpperCase() }}
      </div>
      <div class="signal-meta">
        {{ report.stock_label || report.ticker }} · {{ report.trade_date }}
        <template v-if="elapsedText"> · 耗时 {{ elapsedText }}</template>
      </div>
    </div>

    <el-alert type="info" :closable="false" class="disclaimer">
      ⚠️ 本报告由 AI 自动生成，仅供学习研究，不构成投资建议。
    </el-alert>

    <!-- Downloads -->
    <div v-if="report.ticker && report.trade_date" class="downloads">
      <el-button tag="a" :href="historyApi.markdownUrl(report.ticker, report.trade_date)" target="_blank">
        📥 下载 Markdown
      </el-button>
      <el-button tag="a" :href="historyApi.pdfUrl(report.ticker, report.trade_date)" target="_blank">
        📄 下载 PDF
      </el-button>
    </div>

    <el-divider />

    <!-- Final plan -->
    <section v-if="state.investment_plan">
      <h3 class="section-title">👔 最终投资建议</h3>
      <div class="md-body" v-html="render(state.investment_plan)"></div>
    </section>

    <!-- Analyst reports -->
    <section>
      <h3 class="section-title">📊 分析师报告</h3>
      <el-collapse>
        <el-collapse-item
          v-for="sec in analystSections"
          :key="sec.key"
          :title="sec.title"
        >
          <div class="md-body" v-html="render(state[sec.key])"></div>
        </el-collapse-item>
      </el-collapse>
    </section>

    <!-- Bull/Bear debate -->
    <section v-if="isDebate(state.investment_debate_state)">
      <h3 class="section-title">⚔️ 多空辩论</h3>
      <el-tabs>
        <el-tab-pane label="多方">
          <div class="md-body" v-html="render(state.investment_debate_state.bull_history || '无数据')"></div>
        </el-tab-pane>
        <el-tab-pane label="空方">
          <div class="md-body" v-html="render(state.investment_debate_state.bear_history || '无数据')"></div>
        </el-tab-pane>
        <el-tab-pane label="研究经理">
          <div class="md-body" v-html="render(state.investment_debate_state.judge_decision || '无数据')"></div>
        </el-tab-pane>
      </el-tabs>
    </section>

    <!-- Trader decision -->
    <section v-if="state.trader_investment_decision">
      <h3 class="section-title">💹 交易员决策</h3>
      <el-collapse>
        <el-collapse-item title="💹 交易员决策">
          <div class="md-body" v-html="render(state.trader_investment_decision)"></div>
        </el-collapse-item>
      </el-collapse>
    </section>

    <!-- Risk debate -->
    <section v-if="isDebate(state.risk_debate_state)">
      <h3 class="section-title">🛡️ 风控评估</h3>
      <el-tabs>
        <el-tab-pane label="激进">
          <div class="md-body" v-html="render(state.risk_debate_state.aggressive_history || '无数据')"></div>
        </el-tab-pane>
        <el-tab-pane label="保守">
          <div class="md-body" v-html="render(state.risk_debate_state.conservative_history || '无数据')"></div>
        </el-tab-pane>
        <el-tab-pane label="中性">
          <div class="md-body" v-html="render(state.risk_debate_state.neutral_history || '无数据')"></div>
        </el-tab-pane>
        <el-tab-pane label="风控决策">
          <div class="md-body" v-html="render(state.risk_debate_state.judge_decision || '无数据')"></div>
        </el-tab-pane>
      </el-tabs>
    </section>

    <!-- Data quality -->
    <section v-if="state.data_quality_summary">
      <el-collapse>
        <el-collapse-item title="✅ 数据质量">
          <div class="md-body" v-html="render(state.data_quality_summary)"></div>
        </el-collapse-item>
      </el-collapse>
    </section>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import { historyApi } from '../api/stocks'
import { signalStyle, stripThinkTags } from '../utils/format'

const props = defineProps({
  /** { ticker, stock_label, trade_date, signal, final_state, elapsed? } */
  report: { type: Object, required: true },
})

const md = new MarkdownIt({ html: false, linkify: true })

const state = computed(() => props.report.final_state || {})
const signal = computed(() => props.report.signal || '')
const style = computed(() => signalStyle(signal.value))

const analystSections = [
  { key: 'market_report', title: '📊 技术分析' },
  { key: 'sentiment_report', title: '💬 市场情绪' },
  { key: 'news_report', title: '📰 新闻舆情' },
  { key: 'fundamentals_report', title: '📋 基本面' },
  { key: 'policy_report', title: '🏛️ 政策分析' },
  { key: 'hot_money_report', title: '🔥 游资追踪' },
].filter((s) => state.value[s.key])

const elapsedText = computed(() => {
  const sec = Math.floor(props.report.elapsed ?? 0)
  if (!sec) return ''
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`
})

function render(text) {
  return md.render(stripThinkTags(String(text || '')))
}

function isDebate(value) {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}
</script>

<style scoped>
.signal-card {
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
  border: 1px solid #333;
  border-radius: 16px;
  padding: 28px;
  text-align: center;
  margin-bottom: 14px;
}
.signal-caption {
  color: var(--text-dim);
  letter-spacing: 2px;
  font-size: 0.85rem;
}
.signal-value {
  font-size: 3rem;
  font-weight: 900;
  margin: 6px 0;
}
.signal-meta {
  color: var(--text);
  font-size: 1.05rem;
}
.disclaimer {
  margin-bottom: 12px;
}
.downloads {
  display: flex;
  gap: 12px;
  margin-top: 12px;
}
.section-title {
  margin: 20px 0 10px;
  font-size: 1.05rem;
}
</style>
