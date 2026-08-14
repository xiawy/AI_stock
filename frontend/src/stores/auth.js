import { defineStore } from 'pinia'
import { authApi } from '../api/auth'
import { getToken, setToken } from '../api/request'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: getToken(),
    user: JSON.parse(localStorage.getItem('aistock_user') || 'null'),
  }),

  getters: {
    isLoggedIn: (state) => Boolean(state.token),
  },

  actions: {
    async login(payload) {
      const { data } = await authApi.login(payload)
      this.token = data.access_token
      this.user = data.user
      setToken(this.token)
      localStorage.setItem('aistock_user', JSON.stringify(this.user))
    },

    async register(payload) {
      await authApi.register(payload)
      // Auto-login right after registration for smoother UX.
      await this.login({ username: payload.username, password: payload.password })
    },

    async fetchMe() {
      if (!this.token) return null
      try {
        const { data } = await authApi.me()
        this.user = data
        localStorage.setItem('aistock_user', JSON.stringify(data))
        return data
      } catch {
        this.clear()
        return null
      }
    },

    async logout() {
      try {
        await authApi.logout()
      } catch {
        // Token already invalid — clear locally regardless.
      }
      this.clear()
    },

    clear() {
      this.token = ''
      this.user = null
      setToken('')
      localStorage.removeItem('aistock_user')
    },
  },
})
