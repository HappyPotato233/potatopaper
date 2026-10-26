<script setup lang="ts">
// 应用外壳：280px 分组侧栏 + 56px 顶栏 + 滚动内容区（Stitch 设计稿）
// 保留原有状态：未读数、admin 分组、当前路由选中态、退出登录
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Radar, LayoutDashboard, FolderGit2, MessageSquareText, ScrollText,
  Settings, Users, Cpu, LogOut, RefreshCw, Search, Plus,
} from 'lucide-vue-next'
import { api, type Subscription } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import CapsuleButton from '@/components/CapsuleButton.vue'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const mainNav = [
  { to: '/', label: '工作台', icon: LayoutDashboard },
  { to: '/directions', label: '研究方向', icon: FolderGit2 },
  { to: '/qa', label: '文献问答', icon: MessageSquareText },
  { to: '/logs', label: '追踪日志', icon: ScrollText },
  { to: '/settings', label: '偏好设置', icon: Settings },
]
// 管理端入口：导航底部独立「管理」分组
const adminNav = [
  { to: '/admin/users', label: '用户管理', icon: Users },
  { to: '/admin/models', label: '模型管理', icon: Cpu },
]

const refreshing = ref(false)
async function refreshUnread() {
  refreshing.value = true
  try { await auth.refreshUnread() } finally { refreshing.value = false }
}

// 顶栏：当前日期
const dateLabel = computed(() => {
  const d = new Date()
  const week = ['日', '一', '二', '三', '四', '五', '六'][d.getDay()]
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 · 星期${week}`
})

// 顶栏：数据同步时间（取各方向最近一次跟进，不新增接口）
const lastSync = ref<string | null>(null)
const syncLabel = computed(() => {
  if (!lastSync.value) return '尚未同步'
  return `数据同步至 ${lastSync.value.slice(0, 16).replace('T', ' ')}`
})

async function loadSync() {
  try {
    const subs = await api.get<Subscription[]>('/api/subscriptions')
    const times = subs.map(s => s.last_run_at).filter(Boolean) as string[]
    if (times.length) {
      times.sort()
      lastSync.value = times[times.length - 1]
    }
  } catch { /* 顶栏状态失败不打扰用户 */ }
}

// 顶栏：全局搜索 → 研究方向页；主操作 → 新建方向
const search = ref('')
function submitSearch() {
  const q = search.value.trim()
  router.push({ path: '/directions', query: q ? { q } : {} })
}

function logout() {
  auth.logout()
  router.push('/login')
}

onMounted(() => {
  if (!auth.isLoggedIn) return
  auth.refreshUnread()
  loadSync()
})
</script>

<template>
  <div class="flex h-full min-w-0">
    <!-- 侧栏 -->
    <aside class="app-sidebar flex w-[280px] shrink-0 flex-col">
      <!-- 品牌区 -->
      <div class="flex items-center gap-2.5 px-5 pb-2 pt-6">
        <Radar :size="23" :stroke-width="1.8" class="text-[#1a1a1a]" />
        <div>
          <div class="brand-logo leading-tight">PaperPotato</div>
          <div class="text-[10px] tracking-[0.18em] text-muted">RESEARCH RADAR</div>
        </div>
      </div>

      <!-- 未读概览（点击刷新） -->
      <button class="mx-3 mt-4 flex items-center justify-between rounded-lg border border-line bg-card px-3 py-2.5 text-[12.5px] hover:bg-cream/60"
              @click="refreshUnread">
        <span class="text-muted">未读文献</span>
        <span class="flex items-center gap-1.5">
          <span class="font-semibold" :class="auth.totalUnread > 0 ? 'text-danger' : 'text-muted'">
            {{ auth.totalUnread }}
          </span>
          <RefreshCw :size="12" class="text-muted" :class="{ 'animate-spin': refreshing }" />
        </span>
      </button>

      <!-- 导航 -->
      <nav class="mt-2 flex flex-1 flex-col overflow-y-auto px-3 pb-4">
        <div class="nav-group-label">核心导航</div>
        <router-link v-for="item in mainNav" :key="item.to" :to="item.to"
                     class="nav-item"
                     :class="{ 'nav-item-active': route.path === item.to }">
          <component :is="item.icon" :size="17" :stroke-width="1.8" />
          <span>{{ item.label }}</span>
          <span v-if="item.to === '/' && auth.totalUnread > 0"
                class="ml-auto rounded-full bg-danger px-1.5 text-[10px] leading-[17px] text-white">
            {{ auth.totalUnread }}
          </span>
        </router-link>

        <!-- 管理分组（admin 专属） -->
        <template v-if="auth.isAdmin">
          <div class="nav-group-label">管理</div>
          <router-link v-for="item in adminNav" :key="item.to" :to="item.to"
                       class="nav-item"
                       :class="{ 'nav-item-active': route.path === item.to }">
            <component :is="item.icon" :size="17" :stroke-width="1.8" />
            <span>{{ item.label }}</span>
          </router-link>
        </template>
      </nav>

      <!-- 用户区 -->
      <div class="border-t border-line px-4 py-4">
        <div class="flex items-center gap-2.5">
          <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary text-[13px] font-semibold text-primary-ink">
            {{ (auth.username || '?').slice(0, 1).toUpperCase() }}
          </div>
          <div class="min-w-0 flex-1">
            <div class="truncate text-[13px] font-medium">{{ auth.username }}</div>
            <div class="text-[11px] text-muted">{{ auth.isAdmin ? '管理员' : '用户' }}</div>
          </div>
          <button class="rounded-md p-1.5 text-muted transition-colors hover:bg-cream hover:text-foreground"
                  title="退出登录" @click="logout">
            <LogOut :size="15" />
          </button>
        </div>
      </div>
    </aside>

    <!-- 右侧：顶栏 + 内容区 -->
    <div class="flex min-w-0 flex-1 flex-col">
      <header class="app-topbar shrink-0">
        <div class="text-[12.5px] text-muted">{{ dateLabel }}</div>
        <div class="hidden items-center gap-1.5 text-[12px] text-muted xl:flex">
          <span class="h-1.5 w-1.5 rounded-full bg-ok"></span>
          {{ syncLabel }}
        </div>
        <div class="ml-auto flex items-center gap-2.5">
          <label class="flex h-9 w-[230px] items-center gap-2 rounded-full border border-line bg-card px-3.5">
            <Search :size="14" class="shrink-0 text-muted" />
            <input v-model="search" type="search" placeholder="搜索研究方向…"
                   class="w-full bg-transparent text-[12.5px] outline-none placeholder:text-muted"
                   @keydown.enter="submitSearch" />
          </label>
          <CapsuleButton size="sm" @click="router.push('/directions?new=1')">
            <Plus :size="14" /> 新建方向
          </CapsuleButton>
        </div>
      </header>

      <main class="min-w-0 flex-1 overflow-y-auto">
        <router-view />
      </main>
    </div>
  </div>
</template>
