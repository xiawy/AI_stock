<template>
  <header class="app-header">
    <div class="inner">
      <router-link to="/" class="brand brand-title">
        <span class="accent">AI</span> Stock
        <span class="tagline">A股多Agent投研系统</span>
      </router-link>

      <nav class="nav">
        <router-link to="/">首页</router-link>
        <router-link to="/diagnosis">诊股</router-link>
        <router-link to="/recommendation">荐股</router-link>
        <router-link to="/impact">新闻榜</router-link>
      </nav>

      <el-dropdown v-if="auth.user" @command="onCommand">
        <span class="user-chip">
          <el-icon><UserFilled /></el-icon>
          {{ auth.user.username }}
        </span>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="profile">个人中心</el-dropdown-item>
            <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>
  </header>
</template>

<script setup>
import { UserFilled } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()

async function onCommand(cmd) {
  if (cmd === 'profile') {
    router.push('/profile')
  } else if (cmd === 'logout') {
    await auth.logout()
    router.push('/login')
  }
}
</script>

<style scoped>
.app-header {
  position: sticky;
  top: 0;
  z-index: 100;
  background: rgba(10, 10, 10, 0.85);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--border);
}
.inner {
  max-width: 1200px;
  margin: 0 auto;
  padding: 12px 24px;
  display: flex;
  align-items: center;
  gap: 28px;
}
.brand {
  color: var(--text);
  text-decoration: none;
  font-size: 1.15rem;
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.tagline {
  font-size: 0.72rem;
  font-weight: 400;
  color: var(--text-dim);
  letter-spacing: 1px;
}
.nav {
  display: flex;
  gap: 18px;
  flex: 1;
}
.nav a {
  color: var(--text-dim);
  text-decoration: none;
  font-size: 0.95rem;
}
.nav a.router-link-active {
  color: var(--brand);
}
.user-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text);
  cursor: pointer;
  font-size: 0.95rem;
}
</style>
