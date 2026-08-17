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
    desc: '全市场新闻与政策热点的 AI 影响力评估：4-Agent 并行打分、供需量化、多空辩论三重过滤，输出 Top 20 排名，多空倾向、受益行业、预期涨幅一目了然',
    cta: '浏览新闻榜',
    to: '/impact',
  },
  {
    icon: '🏭',
    title: '行业榜',
    desc: '新闻热度加权聚合到行业维度，叠加主力资金净流入共振识别与 A/B/C 评级；点击行业联动查看龙头股与相关新闻，支持一键深度诊股，捕捉行业β机会',
    cta: '查看行业榜',
    to: '/industry',
  },
  {
    icon: '⭐',
    title: '热股榜',
    desc: '行业龙头 + 事件受益 + 涨停基因三维候选池，个股多空辩论精选 Top 10 正选与 3 备选，附目标价、止损位与持有周期，每日 00:00 / 08:30 / 12:30 / 14:30 定时更新',
    cta: '查看热股榜',
    to: '/recommendation',
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
  grid-template-columns: repeat(4, 1fr); /* four landscape cards in one row */
  gap: 16px;
  padding: 8px 16px 40px;
}
@media (max-width: 960px) {
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
