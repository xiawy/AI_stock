import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('../views/Login.vue'),
    meta: { public: true },
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('../views/Register.vue'),
    meta: { public: true },
  },
  {
    path: '/',
    name: 'home',
    component: () => import('../views/Home.vue'),
  },
  {
    path: '/diagnosis',
    name: 'diagnosis',
    component: () => import('../views/Dashboard.vue'),
  },
  {
    path: '/analysis/:taskId',
    name: 'analysis',
    component: () => import('../views/Analysis.vue'),
    props: true,
  },
  {
    path: '/history/:ticker/:tradeDate',
    name: 'history-report',
    component: () => import('../views/HistoryReport.vue'),
    props: true,
  },
  {
    path: '/profile',
    name: 'profile',
    component: () => import('../views/Profile.vue'),
  },
  {
    path: '/impact',
    name: 'impact',
    component: () => import('../views/ImpactRanking.vue'),
  },
  {
    path: '/industry',
    name: 'industry',
    component: () => import('../views/IndustryRanking.vue'),
  },
  {
    path: '/recommendation',
    name: 'recommendation',
    component: () => import('../views/StockRecommendation.vue'),
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: '/',
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// Route guard: unauthenticated users are redirected to /login.
router.beforeEach((to) => {
  const auth = useAuthStore()
  if (!to.meta.public && !auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  // Already logged in? Skip the auth pages.
  if (to.meta.public && auth.isLoggedIn && (to.name === 'login' || to.name === 'register')) {
    return { name: 'home' }
  }
})

export default router
