<script setup lang="ts">
// 工作台：问候头图 + 4 指标卡 + 方向卡片网格 + 右栏待研读文献 + 文献弹窗 + 手动跟踪
// 手动跟踪：下发后轮询任务进度，全部完成后刷新未读与日志
// 右栏：取未读最多的方向，用 /api/unread/{id} 拉取未读文献，按相关度排序展示前 3 篇
// 文献弹窗：未读在前 + 红点浅底 + 全部已读 + Escape 关闭 + body 滚动锁定
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  FolderGit2, Play, FileText, Inbox, CheckCheck, ExternalLink, Sparkles, Clock,
} from 'lucide-vue-next'
import { api, type Paper, type Subscription } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import CapsuleButton from '@/components/CapsuleButton.vue'
import StatCard from '@/components/StatCard.vue'

const auth = useAuthStore()
const router = useRouter()

const subs = ref<Subscription[]>([])
const loading = ref(true)
const runMsg = ref('')

// 指标卡（全部来自 /api/subscriptions 与未读汇总，不造数字）
const stats = computed(() => ({
  total: subs.value.length,
  running: subs.value.filter(s => s.active).length,
  papers: subs.value.reduce((a, s) => a + s.paper_count, 0),
  unread: auth.totalUnread,
}))

const greeting = computed(() => {
  const h = new Date().getHours()
  return h < 6 ? '凌晨好' : h < 12 ? '早上好' : h < 18 ? '下午好' : '晚上好'
})

// 最近一次跟进时间（取全部方向 last_run_at 的最大值）
const lastSyncLabel = computed(() => {
  const times = subs.value.map(s => s.last_run_at).filter(Boolean) as string[]
  if (times.length === 0) return '—'
  times.sort()
  const iso = times[times.length - 1]
  const d = new Date(iso.replace(' ', 'T'))
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
})

// 右栏：未读最多的方向（其未读文献作为「待研读」列表数据源）
const unreadSubs = computed(() => [...subs.value]
  .filter(s => s.unread_count > 0)
  .sort((a, b) => b.unread_count - a.unread_count))
const focusSub = computed(() => unreadSubs.value[0] ?? null)

const focusPapers = ref<Paper[]>([])
const focusLoading = ref(false)
const focusVisible = computed(() => focusPapers.value.filter(p => !p.is_read).slice(0, 3))

function relTime(iso?: string | null): string {
  if (!iso) return '尚未跟进'
  const t = new Date(iso.replace(' ', 'T')).getTime()
  if (Number.isNaN(t)) return iso.slice(0, 10)
  const min = Math.floor((Date.now() - t) / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const h = Math.floor(min / 60)
  if (h < 24) return `${h} 小时前`
  return `${Math.floor(h / 24)} 天前`
}

function thresholdPct(s: Subscription): number {
  return Math.round((s.threshold ?? 0) * 100)
}

async function loadFocus() {
  const sub = unreadSubs.value[0]
  if (!sub) { focusPapers.value = []; return }
  focusLoading.value = true
  try {
    focusPapers.value = await api.get<Paper[]>(`/api/unread/${sub.id}`)
  } catch {
    focusPapers.value = []
  } finally {
    focusLoading.value = false
  }
}

async function load() {
  loading.value = true
  try {
    subs.value = await api.get<Subscription[]>('/api/subscriptions')
    await auth.refreshUnread()
    await loadFocus()
  } finally { loading.value = false }
}

// ---------- 手动跟踪：下发 + 轮询进度 + 完成刷新 ----------
const followupRunning = ref(false)
const followupProgress = ref('')
let followupTimer: number | null = null

const TERMINAL = new Set(['success', 'partial_failed', 'failed'])

async function runFollowup() {
  const targets = subs.value.filter(x => x.active)
  if (targets.length === 0 || followupRunning.value) return
  followupRunning.value = true
  runMsg.value = ''

  const tasks: { name: string; taskId: string; status: string }[] = []
  for (const s of targets) {
    try {
      const r = await api.post<{ task_id: string }>(`/api/follow-up/run?subscription_id=${s.id}`)
      tasks.push({ name: s.name, taskId: r.task_id, status: '' })
    } catch (e: any) {
      runMsg.value += `「${s.name}」${e.message}；`
    }
  }
  if (tasks.length === 0) { followupRunning.value = false; return }
  followupProgress.value = `跟踪中…（0/${tasks.length} 完成）`

  followupTimer = window.setInterval(async () => {
    let finished = 0
    for (const t of tasks) {
      if (t.status) { finished++; continue }
      try {
        const st = await api.get<{ status: string }>(`/api/follow-up/tasks/${t.taskId}`)
        if (TERMINAL.has(st.status)) t.status = st.status
      } catch { /* 任务行尚未创建，继续等待 */ }
    }
    if (finished < tasks.length) {
      followupProgress.value = `跟踪中…（${finished}/${tasks.length} 完成）`
      return
    }
    stopFollowupPoll()
    const okN = tasks.filter(t => t.status === 'success').length
    const partialN = tasks.filter(t => t.status === 'partial_failed').length
    const failN = tasks.filter(t => t.status === 'failed').length
    runMsg.value += `跟踪完成：成功 ${okN}` +
      (partialN ? `、部分失败 ${partialN}` : '') + (failN ? `、失败 ${failN}` : '')
    await load()
  }, 2000)
}

function stopFollowupPoll() {
  if (followupTimer !== null) { clearInterval(followupTimer); followupTimer = null }
  followupRunning.value = false
  followupProgress.value = ''
}

// ---------- 文献弹窗 ----------
const papersModal = ref<{ sub: Subscription; papers: Paper[] } | null>(null)
const papersLoading = ref(false)

const sortedPapers = computed(() => {
  const list = papersModal.value?.papers ?? []
  return [...list].sort((a, b) => Number(a.is_read) - Number(b.is_read))
})

async function openPapers(sub: Subscription) {
  papersModal.value = { sub, papers: [] }
  papersLoading.value = true
  try {
    papersModal.value.papers = await api.get<Paper[]>(`/api/subscriptions/${sub.id}/papers`)
  } finally { papersLoading.value = false }
}

function closePapers() { papersModal.value = null }

// 点击文献卡片：进入系统内置阅读页（不跳转原文）
function openReader(paperId: number) {
  document.body.style.overflow = ''
  papersModal.value = null
  router.push(`/papers/${paperId}`)
}

async function markRead(paperId: number) {
  await api.post(`/api/papers/${paperId}/read`)
  if (papersModal.value) {
    const p = papersModal.value.papers.find(x => x.id === paperId)
    if (p) p.is_read = true
  }
  const fp = focusPapers.value.find(x => x.id === paperId)
  if (fp) fp.is_read = true
  auth.refreshUnread()
}

async function markAllRead() {
  const m = papersModal.value
  if (!m) return
  const unreadIds = m.papers.filter(p => !p.is_read).map(p => p.id)
  if (unreadIds.length === 0) return
  await api.post('/api/papers/batch-read', { subscription_id: m.sub.id, paper_ids: unreadIds })
  m.papers.forEach(p => { p.is_read = true })
  focusPapers.value.forEach(p => { if (m.papers.some(x => x.id === p.id)) p.is_read = true })
  auth.refreshUnread()
}

// 右栏直接标记已读（不打开弹窗）
async function markFocusRead(paperId: number) {
  await api.post(`/api/papers/${paperId}/read`)
  const p = focusPapers.value.find(x => x.id === paperId)
  if (p) p.is_read = true
  auth.refreshUnread()
}

async function markAllFocusRead() {
  const sub = focusSub.value
  if (!sub) return
  const ids = focusPapers.value.filter(p => !p.is_read).map(p => p.id)
  if (ids.length === 0) return
  await api.post('/api/papers/batch-read', { subscription_id: sub.id, paper_ids: ids })
  focusPapers.value.forEach(p => { p.is_read = true })
  auth.refreshUnread()
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && papersModal.value) closePapers()
}
watch(papersModal, (m) => {
  document.body.style.overflow = m ? 'hidden' : ''
})
onMounted(() => {
  load()
  window.addEventListener('keydown', onKeydown)
})
onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  document.body.style.overflow = ''
  stopFollowupPoll()
})
</script>

<template>
  <div class="page">
    <!-- 问候头图 -->
    <section class="mb-6 flex flex-col gap-4 rounded-xl border border-line bg-card px-6 py-5 md:flex-row md:items-center md:justify-between">
      <div class="min-w-0">
        <div class="page-eyebrow">文献动态简报 · DAILY RADAR DISPATCH</div>
        <h1 class="mt-1.5 font-brand text-[24px] font-semibold leading-tight">
          {{ greeting }}，{{ auth.username || '研究员' }}。
        </h1>
        <p class="mt-1 text-[12.5px] text-muted">
          <template v-if="stats.unread > 0">
            今日有 {{ stats.unread }} 篇文献待研读，来自 {{ stats.running }} 个运行中的方向，累计已建档 {{ stats.papers }} 篇。
          </template>
          <template v-else>
            当前 {{ stats.running }} 个方向在运行，未读已清空，可随时发起新一轮跟踪。
          </template>
        </p>
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <CapsuleButton variant="ghost" size="sm" @click="router.push('/directions?new=1')">
          <FolderGit2 :size="14" /> 新建方向
        </CapsuleButton>
        <CapsuleButton size="sm" :disabled="stats.running === 0 || followupRunning" @click="runFollowup">
          <template v-if="followupRunning">
            <Play :size="14" class="animate-pulse" /> {{ followupProgress || '跟踪中…' }}
          </template>
          <template v-else>
            <Play :size="14" /> 执行即时跟踪
          </template>
        </CapsuleButton>
      </div>
    </section>

    <div v-if="runMsg" class="mb-4 rounded-lg border border-line bg-softgreen px-4 py-2 text-[12.5px] text-[#3d5c4a]">
      {{ runMsg }}
    </div>

    <!-- 指标卡 -->
    <div class="mb-8 grid grid-cols-2 gap-3.5 lg:grid-cols-4">
      <StatCard label="活跃研究方向" :value="stats.running" :icon="FolderGit2"
                :hint="`共 ${stats.total} 个方向在册`" />
      <StatCard label="最近一次同步" :value="lastSyncLabel" :icon="Clock"
                :hint="`${stats.running} 个方向参与`" />
      <StatCard label="已建档文献总量" :value="stats.papers" :icon="FileText"
                :hint="`覆盖 ${stats.total} 个方向`" />
      <StatCard label="待研读未读" :value="stats.unread" :icon="Inbox"
                :hint="stats.unread > 0 ? '需处理' : '已全部读完'"
                :accent="stats.unread > 0 ? 'danger' : 'default'" />
    </div>

    <div class="grid gap-6 lg:grid-cols-12">
      <!-- 左：方向卡片 -->
      <section class="lg:col-span-8">
        <div class="mb-3 flex items-baseline justify-between">
          <h2 class="section-title">研究课题跟踪概览</h2>
          <span class="text-[12px] text-muted">共 {{ subs.length }} 个方向</span>
        </div>
        <div v-if="loading" class="panel p-10 text-center text-[13px] text-muted">加载中…</div>
        <div v-else-if="subs.length === 0" class="panel p-10 text-center text-[13px] text-muted">
          暂无研究方向，点击右上角「新建方向」开始订阅
        </div>
        <div v-else class="grid gap-3.5 md:grid-cols-2">
          <article v-for="s in subs" :key="s.id"
                   class="panel flex flex-col p-5 transition-colors hover:border-primary-deep">
            <div class="mb-2 flex items-center justify-between gap-2 text-[11.5px] text-muted">
              <span class="flex items-center gap-1.5">
                <span class="h-1.5 w-1.5 rounded-full" :class="s.active ? 'bg-ok' : 'bg-muted/50'"></span>
                {{ relTime(s.last_run_at) }}{{ s.active ? '更新' : ' · 已暂停' }}
              </span>
              <span>阈值 ≥ {{ thresholdPct(s) }}%</span>
            </div>

            <h3 class="font-brand text-[16.5px] font-semibold leading-snug">{{ s.name }}</h3>

            <div class="mt-2.5 flex flex-wrap gap-1.5">
              <span v-for="k in s.keywords.slice(0, 4)" :key="k" class="chip">{{ k }}</span>
              <span v-if="s.keywords.length > 4" class="chip">+{{ s.keywords.length - 4 }}</span>
            </div>

            <div class="mt-auto flex items-center justify-between border-t border-line pt-3 text-[12px]">
              <span class="text-muted">总计 <b class="font-semibold text-foreground">{{ s.paper_count }}</b> 篇</span>
              <span :class="s.unread_count > 0 ? 'font-semibold text-danger' : 'text-muted'">
                未读 {{ s.unread_count }} 篇
              </span>
            </div>

            <div class="mt-3 flex items-center gap-2">
              <div class="flex min-w-0 flex-1 flex-wrap gap-1">
                <span v-for="src in s.sources.slice(0, 2)" :key="src"
                      class="truncate text-[10.5px] text-muted">#{{ src }}</span>
                <span v-if="s.sources.length > 2" class="text-[10.5px] text-muted">+{{ s.sources.length - 2 }}</span>
              </div>
              <CapsuleButton size="sm" variant="ghost" @click="openPapers(s)">查询文献</CapsuleButton>
            </div>
          </article>
        </div>
      </section>

      <!-- 右：待研读文献（真实未读数据） -->
      <aside class="lg:col-span-4">
        <section class="panel flex flex-col p-5">
          <div class="flex items-center justify-between gap-2">
            <h3 class="section-title flex items-center gap-2">
              <Inbox :size="16" class="text-primary-ink" />
              待研读文献
              <span class="chip chip-danger !py-0.5">{{ auth.totalUnread }} 篇</span>
            </h3>
            <button v-if="focusPapers.some(p => !p.is_read)"
                    class="rounded p-1 text-muted hover:bg-cream hover:text-foreground"
                    title="全部标记已读" @click="markAllFocusRead">
              <CheckCheck :size="15" />
            </button>
          </div>
          <p class="mb-1 mt-1 text-[11.5px] text-muted">
            <template v-if="focusSub">来自「{{ focusSub.name }}」· 按相关度排序</template>
            <template v-else>所有方向均已读完</template>
          </p>

          <div v-if="focusLoading" class="py-10 text-center text-[12.5px] text-muted">加载中…</div>
          <div v-else-if="focusVisible.length === 0" class="py-12 text-center text-[12.5px] text-muted">
            {{ focusSub ? '该方向已全部读完' : '暂无未读文献' }}
          </div>
          <ul v-else class="flex flex-col">
            <li v-for="(p, i) in focusVisible" :key="p.id"
                class="border-b border-line/70 py-4 last:border-0">
              <div class="flex items-center gap-2 text-[10.5px]">
                <span class="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-cream font-mono text-[10px]">
                  {{ i + 1 }}
                </span>
                <span class="chip chip-ok !py-0.5">相关度 {{ Math.round(p.score * 100) }}%</span>
                <span class="truncate font-mono text-muted">{{ p.source }}</span>
                <span class="ml-auto shrink-0 text-muted">{{ p.published_at?.slice(0, 10) ?? '' }}</span>
              </div>

              <a v-if="p.url" :href="p.url" target="_blank" rel="noopener"
                 class="mt-2 block font-brand text-[14.5px] font-semibold leading-snug hover:underline">
                {{ p.title }}
              </a>
              <div v-else class="mt-2 font-brand text-[14.5px] font-semibold leading-snug">{{ p.title }}</div>

              <div v-if="p.authors.length" class="mt-1 truncate text-[11px] text-muted">
                {{ p.authors.join(', ') }}
              </div>
              <p class="mt-1.5 line-clamp-2 text-[12px] leading-relaxed text-muted">{{ p.abstract }}</p>

              <div class="mt-2 flex flex-wrap items-center gap-1">
                <span v-if="p.arxiv_cat" class="badge badge-muted !py-0">{{ p.arxiv_cat }}</span>
                <span v-if="p.journal_level" class="badge badge-primary !py-0">{{ p.journal_level }}</span>
                <span v-if="p.ccf" class="badge badge-primary !py-0">CCF-{{ p.ccf }}</span>
                <span v-if="p.cas_zone" class="badge badge-primary !py-0">{{ p.cas_zone }}</span>
                <a v-if="p.url" :href="p.url" target="_blank" rel="noopener"
                   class="ml-auto rounded p-1 text-muted hover:bg-cream hover:text-foreground" title="打开原文">
                  <ExternalLink :size="13" />
                </a>
                <button class="text-[11px] text-muted hover:text-foreground" @click="markFocusRead(p.id)">
                  标记已读
                </button>
              </div>
            </li>
          </ul>

          <CapsuleButton v-if="focusSub" variant="ghost" size="sm" class="mt-4 w-full"
                         @click="openPapers(focusSub)">
            <Sparkles :size="13" /> 查看「{{ focusSub.name }}」全部文献
          </CapsuleButton>
        </section>
      </aside>
    </div>

    <!-- 文献弹窗 -->
    <div v-if="papersModal" class="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-6"
         @click.self="closePapers">
      <div class="panel flex max-h-[82vh] w-[780px] flex-col overflow-hidden">
        <div class="flex items-center justify-between border-b border-line px-6 py-4">
          <div>
            <div class="page-eyebrow !text-[10px]">DIRECTION PAPERS</div>
            <h3 class="mt-0.5 text-[16px] font-semibold">{{ papersModal.sub.name }}</h3>
          </div>
          <div class="flex items-center gap-2">
            <CapsuleButton v-if="papersModal.papers.some(p => !p.is_read)"
                           size="sm" variant="ghost" @click="markAllRead">
              <CheckCheck :size="13" /> 全部标记已读
            </CapsuleButton>
            <button class="rounded-md px-2 py-1 text-muted hover:bg-cream hover:text-foreground"
                    @click="closePapers">✕</button>
          </div>
        </div>
        <div class="flex-1 overflow-y-auto px-6 py-4">
          <div v-if="papersLoading" class="py-10 text-center text-[13px] text-muted">加载中…</div>
          <div v-else-if="papersModal.papers.length === 0" class="py-10 text-center text-[13px] text-muted">
            暂无入库文献
          </div>
          <div v-else class="flex flex-col gap-3">
            <div v-for="p in sortedPapers" :key="p.id"
                 class="group cursor-pointer rounded-lg border p-4 transition-all duration-200 ease-out hover:-translate-y-0.5 hover:border-primary-deep hover:bg-cream/70 hover:shadow-[0_10px_22px_rgba(95,87,57,0.12)]"
                 :class="p.is_read ? 'border-line' : 'border-line bg-cream/40'"
                 @click="openReader(p.id)">
              <div class="mb-1 flex items-start justify-between gap-3">
                <div class="flex min-w-0 items-start gap-2">
                  <span v-if="!p.is_read" class="mt-[7px] h-2 w-2 shrink-0 rounded-full bg-danger"
                        title="未读"></span>
                  <span class="text-[14.5px] font-medium leading-snug transition-colors group-hover:text-primary-ink">
                    {{ p.title }}
                  </span>
                </div>
                <CapsuleButton v-if="!p.is_read" size="sm" variant="ghost" class="shrink-0"
                               @click.stop="markRead(p.id)">
                  <CheckCheck :size="13" /> 已读
                </CapsuleButton>
                <span v-else class="shrink-0 text-[12px] text-muted">已读</span>
              </div>
              <div class="mb-2 flex flex-wrap items-center gap-2 text-[11.5px] text-muted">
                <span class="chip">[{{ p.source }}]</span>
                <span v-if="p.journal">{{ p.journal }}</span>
                <span v-if="p.published_at">{{ p.published_at.slice(0, 10) }}</span>
                <span v-if="p.journal_level" class="badge badge-primary !py-0">{{ p.journal_level }}</span>
                <span v-if="p.ccf" class="badge badge-primary !py-0">CCF-{{ p.ccf }}</span>
                <span v-if="p.cas_zone" class="badge badge-primary !py-0">{{ p.cas_zone }}</span>
                <span v-if="p.arxiv_cat" class="badge badge-muted !py-0">{{ p.arxiv_cat }}</span>
                <span class="ml-auto">相关度 {{ (p.score * 100).toFixed(0) }}%</span>
              </div>
              <p class="line-clamp-2 text-[12.5px] leading-relaxed text-muted">{{ p.abstract }}</p>
            </div>
          </div>
        </div>
        <div class="border-t border-line px-6 py-3">
          <CapsuleButton variant="ghost" size="sm" class="w-full"
                         @click="router.push(`/qa?review=${papersModal?.sub.id}`)">
            <Sparkles :size="14" /> 对该方向生成多视角综述
          </CapsuleButton>
        </div>
      </div>
    </div>
  </div>
</template>
