// 认证 store：token / 用户信息 / 未读数（登录快路径返回）
import { defineStore } from 'pinia'
import { api, type UnreadItem } from '@/api/client'

interface State {
  token: string
  username: string
  userId: number
  role: 'user' | 'admin'
  totalUnread: number
  unreadBrief: UnreadItem[]
}

export const useAuthStore = defineStore('auth', {
  state: (): State => ({
    token: localStorage.getItem('token') ?? '',
    username: localStorage.getItem('username') ?? '',
    userId: Number(localStorage.getItem('userId') ?? 0),
    role: (localStorage.getItem('role') as 'user' | 'admin') ?? 'user',
    totalUnread: 0,
    unreadBrief: [],
  }),
  getters: {
    isLoggedIn: (s) => !!s.token,
    isAdmin: (s) => s.role === 'admin',
  },
  actions: {
    async login(username: string, password: string) {
      const data = await api.post<{
        access_token: string; role: string; username: string;
        user_id: number; total_unread: number; unread_brief: UnreadItem[]
      }>('/api/auth/login', { username, password })
      this.token = data.access_token
      this.username = data.username
      this.userId = data.user_id
      this.role = data.role as 'user' | 'admin'
      this.totalUnread = data.total_unread
      this.unreadBrief = data.unread_brief
      localStorage.setItem('token', this.token)
      localStorage.setItem('username', this.username)
      localStorage.setItem('userId', String(this.userId))
      localStorage.setItem('role', this.role)
    },
    async refreshUnread() {
      const data = await api.get<{ total_unread: number; items: UnreadItem[] }>('/api/unread/summary')
      this.totalUnread = data.total_unread
      this.unreadBrief = data.items
    },
    logout() {
      api.post('/api/auth/logout').catch(() => {})
      this.token = ''
      this.username = ''
      this.userId = 0
      this.role = 'user'
      localStorage.clear()
    },
  },
})
