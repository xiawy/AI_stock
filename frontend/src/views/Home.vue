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

      <!-- Feature cards -->
      <section class="cards">
        <router-link v-for="card in cards" :key="card.to" :to="card.to" class="card">
          <div class="card-icon">{{ card.icon }}</div>
          <h3 class="card-title">{{ card.title }}</h3>
          <p class="card-desc">{{ card.desc }}</p>
          <span class="card-cta">{{ card.cta }} →</span>
        </router-link>
      </section>
    </div>
  </div>
</template>

<script setup>
import AppHeader from '../components/AppHeader.vue'

const cards = [
  {
    icon: '🔍',
    title: '诊股',
    desc: '多 Agent 协同深度分析个股，质量门控 + 多空辩论 + 风控评估，输出决策建议',
    cta: '开始诊股',
    to: '/diagnosis',
  },
  {
    icon: '⭐',
    title: '寻龙榜',
    desc: '事件驱动精选组合，Top 10 正选 + 3 备选，每日 08:00 / 11:30 / 14:30 定时更新',
    cta: '查看寻龙榜',
    to: '/recommendation',
  },
  {
    icon: '📰',
    title: '新闻榜',
    desc: '热点新闻影响力评估与排名，多空倾向、受益行业、预期涨幅一目了然',
    cta: '浏览新闻榜',
    to: '/impact',
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
  grid-template-columns: repeat(auto-fit, minmax(240px, 300px));
  justify-content: center;
  gap: 20px;
  padding: 8px 16px 40px;
}
.card {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 30px 24px 26px;
  text-decoration: none;
  transition: transform 0.22s ease, border-color 0.22s ease, box-shadow 0.22s ease;
}
.card:hover {
  transform: translateY(-6px);
  border-color: var(--brand);
  box-shadow: 0 12px 32px rgba(255, 90, 31, 0.14);
}
.card-icon {
  font-size: 2.2rem;
  width: 72px;
  height: 72px;
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
.card-title {
  margin: 0 0 10px;
  font-size: 1.2rem;
  color: var(--text);
}
.card-desc {
  margin: 0 0 18px;
  color: var(--text-dim);
  font-size: 0.85rem;
  line-height: 1.7;
  flex: 1;
}
.card-cta {
  color: var(--brand);
  font-size: 0.9rem;
  font-weight: 600;
}
</style>
