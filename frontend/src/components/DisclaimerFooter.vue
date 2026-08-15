<template>
  <footer class="disclaimer">
    ⚠️ 本项目仅供学习研究与技术演示，不构成任何投资建议。投资决策请咨询持牌专业机构。
  </footer>
</template>

<style scoped>
/* Pinned to the viewport bottom on every page; body has matching padding
   (see main.css) so scrolling content never slides under it. */
.disclaimer {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 200;
  text-align: center;
  color: #555;
  font-size: 0.75rem;
  line-height: 1.6;
  background: var(--bg);
  border-top: 1px solid #1a1a1a;
  padding: 8px 16px;
}
</style>
