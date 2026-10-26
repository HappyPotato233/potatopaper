// 路由：8 视图 + 登录守卫 + /admin/** RBAC 守卫（前端设计文档 §5.8）
import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue') },
    {
      path: '/',
      component: () => import('@/layouts/AppLayout.vue'),
      children: [
        { path: '', name: 'dashboard', component: () => import('@/views/DashboardView.vue') },
        { path: 'directions', name: 'directions', component: () => import('@/views/SubscriptionsView.vue') },
        { path: 'qa', name: 'qa', component: () => import('@/views/QaView.vue') },
        { path: 'papers/:id', name: 'paper-reader', component: () => import('@/views/PaperReaderView.vue') },
        { path: 'logs', name: 'logs', component: () => import('@/views/LogsView.vue') },
        { path: 'settings', name: 'settings', component: () => import('@/views/SettingsView.vue') },
        { path: 'admin/users', name: 'admin-users', component: () => import('@/views/admin/UsersView.vue'),
          meta: { admin: true } },
        { path: 'admin/models', name: 'admin-models', component: () => import('@/views/admin/ModelsView.vue'),
          meta: { admin: true } },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.name !== 'login' && !auth.isLoggedIn) return { name: 'login' }
  if (to.name === 'login' && auth.isLoggedIn) return { name: 'dashboard' }
  // 管理端守卫：非 admin 重定向首页（后端仍会 403 双保险）
  if (to.meta.admin && !auth.isAdmin) return { name: 'dashboard' }
})

export default router
