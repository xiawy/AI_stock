<template>
  <div>
    <AppHeader />
    <div class="page home">
      <!-- Hero -->
      <section class="hero">
        <div class="hero-badge">AI POWERED</div>
        <h1 class="hero-title brand-title"><span class="accent">AI</span> Stock</h1>
        <p class="hero-sub">A股多 Agent 智能投研系统</p>
      </section>

      <!-- Feature cards — one row of four landscape cards -->
      <section class="cards">
        <router-link v-for="card in cards" :key="card.to" :to="card.to" class="card">
          <div class="card-icon">{{ card.icon }}</div>
          <div class="card-body">
            <h3 class="card-title">{{ card.title }}</h3>
            <p class="card-desc">{{ card.desc }}</p>
            <span class="card-cta">{{ card.cta }} →</span>
          </div>
        </router-link>
      </section>
    </div>
  </div>
</template>

<script setup>
import AppHeader from '../components/AppHeader.vue'

const cards = [
  {
    icon: '📰',
    title: '新闻榜',
    desc: '全市场新闻与政策热点的主题级评估：事件分类 + 重要度 + L3 赛道方向 + 预期幅度，按类型配额选 Top 20（避免政策刷屏），同事件多源只占一个榜位；带模型/规则来源徽章、跨期变化与命中率成绩单',
    cta: '浏览新闻榜',
    to: '/impact',
  },
  {
    icon: '🏭',
    title: '行业榜',
    desc: '量化选股流程按产业生命周期优选的前 10 行业，每行业附龙头股（「领涨」＝当日涨幅第一且跑赢板块，「精选」＝已入池）与关联新闻；带 Δrank / 新进 / 连榜天数与证据链，支持一键深度诊股',
    cta: '查看行业榜',
    to: '/industry',
  },
  {
    icon: '⭐',
    title: '热股榜',
    desc: '自选观察池股票，按动态置信度排序（与买入候选队列同一口径，不再是入池时的静态分）：并列展示入池分与衰减状态、鱼尾标记、到期天数与新进/掉榜变化',
    cta: '查看热股榜',
    to: '/recommendation',
  },
  {
    icon: '🧭',
    title: '策略台',
    desc: '收盘后的市场级研判：今天涨的是哪一拨（板块内结构拆解）· 这拨能不能持续（资金/新闻/结构三方交叉验证 + 高标溯源）· 上次判断错在哪（断言自动回填与打分），结论以权重乘子回灌次日选股',
    cta: '进入策略台',
    to: '/strategy',
  },
  {
    icon: '📈',
    title: '量化',
    desc: '自选池 · 持仓池 · 交易历史三视图；多 Agent 引擎选股/建仓/止损决策自动同步到每个用户的模拟账户，逐笔记录资金变动与盈亏胜率，支持手动加减仓，预留实盘对接',
    cta: '进入量化',
    to: '/quant',
  },
  {
    icon: '🔍',
    title: '诊股',
    desc: '7 位分析师研报 + 质量门控 + 多空辩论 + 风控评估的完整多 Agent 流水线，自动注入行业榜热度上下文，输出买入/观望决策与完整分析报告',
    cta: '开始诊股',
    to: '/diagnosis',
  },
]
</script>

<style scoped>
.home {
  min-height: calc(100vh - 57px);
}
.hero {
  text-align: center;
  padding: 56px 16px 36px;
}
.hero-badge {
  display: inline-block;
  font-size: 0.7rem;
  font-weight: 700;
  letter-spacing: 3px;
  color: var(--brand);
  border: 1px solid var(--brand);
  border-radius: 999px;
  padding: 4px 14px;
  margin-bottom: 18px;
  opacity: 0.9;
}
.hero-title {
  font-size: 2.6rem;
  margin: 0 0 12px;
}
.hero-sub {
  color: var(--text-dim);
  font-size: 1.05rem;
  margin: 0;
  letter-spacing: 2px;
}

.cards {
  display: grid;
  grid-template-columns: repeat(5, 1fr); /* five landscape cards in one row */
  gap: 16px;
  padding: 8px 16px 40px;
}
@media (max-width: 1100px) {
  .cards {
    grid-template-columns: repeat(2, 1fr);
  }
}
@media (max-width: 560px) {
  .cards {
    grid-template-columns: 1fr;
  }
}
.card {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 28px 20px 24px;
  min-height: 340px; /* portrait rectangle: taller than the ~276px column width */
  text-decoration: none;
  transition: transform 0.22s ease, border-color 0.22s ease, box-shadow 0.22s ease;
}
.card:hover {
  transform: translateY(-6px);
  border-color: var(--brand);
  box-shadow: 0 12px 32px rgba(255, 90, 31, 0.14);
}
.card-icon {
  font-size: 2rem;
  width: 64px;
  height: 64px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: var(--bg-card);
  border: 1px solid var(--border);
  margin-bottom: 16px;
  transition: transform 0.22s ease;
}
.card:hover .card-icon {
  transform: scale(1.08);
}
.card-body {
  display: flex;
  flex-direction: column;
  align-items: center;
  flex: 1;
  width: 100%;
  min-width: 0;
}
.card-title {
  margin: 0 0 10px;
  font-size: 1.15rem;
  color: var(--text);
}
.card-desc {
  margin: 0 0 16px;
  color: var(--text-dim);
  font-size: 0.84rem;
  line-height: 1.85;
  flex: 1;
}
.card-cta {
  color: var(--brand);
  font-size: 0.85rem;
  font-weight: 600;
}
</style>
