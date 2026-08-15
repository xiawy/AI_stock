<template>
  <div class="auth-wrap">
    <el-card class="auth-card">
      <div class="brand brand-title" style="font-size: 1.8rem; text-align: center">
        <span class="accent">AI</span> Stock
      </div>
      <div class="subtitle">创建账号</div>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        size="large"
        @keyup.enter="submit"
      >
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="3-32位，字母/数字/中文" />
        </el-form-item>
        <el-form-item label="邮箱" prop="email">
          <el-input v-model="form.email" placeholder="name@example.com" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            placeholder="至少 8 位"
          />
        </el-form-item>
        <el-form-item label="确认密码" prop="confirm">
          <el-input
            v-model="form.confirm"
            type="password"
            show-password
            placeholder="再次输入密码"
          />
        </el-form-item>
        <el-button
          type="primary"
          class="submit-btn"
          :loading="loading"
          @click="submit"
        >
          注 册
        </el-button>
      </el-form>

      <div class="footer-link">
        已有账号？
        <router-link to="/login">直接登录</router-link>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()

const formRef = ref()
const loading = ref(false)
const form = reactive({ username: '', email: '', password: '', confirm: '' })

const rules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 32, message: '长度 3-32 个字符', trigger: 'blur' },
  ],
  email: [
    { required: true, message: '请输入邮箱', trigger: 'blur' },
    { type: 'email', message: '邮箱格式不正确', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 8, message: '密码至少 8 位', trigger: 'blur' },
  ],
  confirm: [
    {
      validator: (_, value, callback) =>
        value === form.password
          ? callback()
          : callback(new Error('两次输入的密码不一致')),
      trigger: 'blur',
    },
  ],
}

async function submit() {
  await formRef.value.validate()
  loading.value = true
  try {
    await auth.register({
      username: form.username,
      email: form.email,
      password: form.password,
    })
    ElMessage.success('注册成功，已自动登录')
    router.push('/')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.auth-wrap {
  /* 56px keeps the centered card clear of the fixed bottom disclaimer. */
  min-height: calc(100vh - 56px);
  display: flex;
  align-items: center;
  justify-content: center;
  background:
    radial-gradient(1200px 600px at 20% -10%, rgba(255, 90, 31, 0.08), transparent),
    var(--bg);
}
.auth-card {
  width: 400px;
  border: 1px solid var(--border);
  border-radius: 16px;
  background: var(--bg-panel);
}
.subtitle {
  text-align: center;
  color: var(--text-dim);
  margin: 6px 0 22px;
  font-size: 0.9rem;
}
.submit-btn {
  width: 100%;
  font-weight: 700;
  letter-spacing: 0.3em;
}
.footer-link {
  margin-top: 16px;
  text-align: center;
  color: var(--text-dim);
  font-size: 0.9rem;
}
.footer-link a {
  color: var(--brand);
  text-decoration: none;
}
</style>
