<template>
  <div>
    <AppHeader />
    <div class="page">
      <div class="page-header">
        <h2>个人中心</h2>
        <div class="header-actions">
          <el-button :icon="ArrowLeft" @click="router.push('/')">返回首页</el-button>
        </div>
      </div>

      <div class="panel profile-card">
        <div class="avatar">{{ initial }}</div>
        <div class="user-name">{{ user?.username || '—' }}</div>
        <div class="user-email">{{ user?.email || '' }}</div>

        <el-divider />

        <el-descriptions :column="1" border class="info">
          <el-descriptions-item label="用户名">
            {{ user?.username || '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="邮箱">
            {{ user?.email || '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="用户 ID">
            {{ user?.id ?? '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="注册时间">
            {{ formatTime(user?.created_at) }}
          </el-descriptions-item>
        </el-descriptions>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowLeft } from '@element-plus/icons-vue'
import AppHeader from '../components/AppHeader.vue'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()

const user = computed(() => auth.user)
const initial = computed(() => (auth.user?.username || '?').charAt(0).toUpperCase())

onMounted(() => {
  // Refresh the profile from the backend (best-effort).
  auth.fetchMe()
})

function formatTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('zh-CN')
}
</script>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
}
.page-header h2 {
  margin: 0;
  font-size: 1.3rem;
  padding-left: 10px;
  border-left: 3px solid var(--brand);
}
.profile-card {
  max-width: 520px;
  margin: 0 auto;
  padding: 36px 32px 28px;
  text-align: center;
}
.avatar {
  width: 84px;
  height: 84px;
  margin: 0 auto 16px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 2rem;
  font-weight: 800;
  color: #fff;
  background: linear-gradient(135deg, var(--brand) 0%, var(--brand-soft) 100%);
  box-shadow: 0 8px 24px rgba(255, 90, 31, 0.25);
}
.user-name {
  font-size: 1.4rem;
  font-weight: 700;
}
.user-email {
  color: var(--text-dim);
  font-size: 0.9rem;
  margin-top: 6px;
}
.info {
  text-align: left;
  margin-top: 4px;
}
</style>
