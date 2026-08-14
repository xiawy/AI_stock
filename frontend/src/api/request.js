import axios from 'axios'
import { ElMessage } from 'element-plus'

const TOKEN_KEY = 'aistock_token'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

const request = axios.create({
  baseURL: '/api',
  timeout: 60000,
})

// Attach JWT on every request
request.interceptors.request.use((config) => {
  const token = getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// Global error handling: 401 → back to login
request.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error.response?.status
    const detail = error.response?.data?.detail

    if (status === 401) {
      setToken('')
      if (!location.pathname.startsWith('/login')) {
        ElMessage.error('登录已过期，请重新登录')
        location.href = '/login'
      }
    } else if (detail) {
      // FastAPI validation errors may be an array of objects
      const msg = Array.isArray(detail)
        ? detail.map((d) => d.msg || JSON.stringify(d)).join('; ')
        : String(detail)
      ElMessage.error(msg)
    }
    return Promise.reject(error)
  },
)

export default request
