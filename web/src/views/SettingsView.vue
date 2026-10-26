<script setup lang="ts">
// 设置：跟进频率四选一 + 通知开关 + 全局采集来源（§2.5，PUT /api/settings 持久化）
import { onMounted, ref } from 'vue'
import { Bell, Clock, Globe2 } from 'lucide-vue-next'
import { api, ALL_SOURCES, FREQ_OPTIONS } from '@/api/client'
import CapsuleButton from '@/components/CapsuleButton.vue'
import PageHeader from '@/components/PageHeader.vue'

const frequency = ref('daily')
const notify = ref({ email: false, push: false })
const sources = ref<string[]>([...ALL_SOURCES])
const loading = ref(true)
const saving = ref(false)
const savedMsg = ref('')
const errorMsg = ref('')

// 页内锚点导航：右侧三张卡对应三个区块
const SECTIONS = [
  { id: 'frequency', label: '跟进频率', icon: Clock },
  { id: 'notify', label: '通知方式', icon: Bell },
  { id: 'sources', label: '采集来源', icon: Globe2 },
]
const activeSection = ref('frequency')

function scrollTo(id: string) {
  activeSection.value = id
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function toggleSource(src: string) {
  sources.value = sources.value.includes(src)
    ? sources.value.filter(x => x !== src)
    : [...sources.value, src]
}

async function load() {
  loading.value = true
  try {
    const s = await api.get<{ frequency: string; notify: { email: boolean; push: boolean }; sources: string[] }>(
      '/api/settings')
    frequency.value = s.frequency
    notify.value = s.notify
    sources.value = s.sources.length ? s.sources : [...ALL_SOURCES]
  } finally { loading.value = false }
}

async function save() {
  if (sources.value.length === 0) { errorMsg.value = '至少保留一个采集来源'; return }
  saving.value = true
  savedMsg.value = ''
  errorMsg.value = ''
  try {
    await api.put('/api/settings', { frequency: frequency.value, notify: notify.value, sources: sources.value })
    savedMsg.value = '已保存'
    setTimeout(() => { savedMsg.value = '' }, 2500)
  } catch (e: any) { errorMsg.value = e.message }
  finally { saving.value = false }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <PageHeader eyebrow="Settings · 偏好设置"
                title="偏好设置"
                subtitle="调度频率、简报推送与默认采集来源，保存后对全部研究方向生效" />

    <div v-if="loading" class="panel p-10 text-center text-[13px] text-muted">加载中…</div>

    <div v-else class="grid gap-6 lg:grid-cols-12">
      <!-- 左：页内导航 -->
      <aside class="lg:col-span-3">
        <div class="panel sticky top-[72px] p-2">
          <button v-for="s in SECTIONS" :key="s.id"
                  class="nav-item w-full text-left"
                  :class="{ 'nav-item-active': activeSection === s.id }"
                  @click="scrollTo(s.id)">
            <component :is="s.icon" :size="15" />
            {{ s.label }}
          </button>
        </div>
      </aside>

      <!-- 右：三张卡 -->
      <div class="flex flex-col gap-5 lg:col-span-9">
        <!-- 自动跟进频率 -->
        <section id="frequency" class="panel scroll-mt-[72px] p-6">
          <h3 class="section-title">自动跟进频率</h3>
          <p class="mb-4 mt-1 text-[12.5px] text-muted">调度器按此频率为所有运行中的方向下发跟进任务</p>
          <div class="segmented flex-wrap">
            <button v-for="f in FREQ_OPTIONS" :key="f.value"
                    class="segmented-item"
                    :class="{ 'segmented-item-active': frequency === f.value }"
                    @click="frequency = f.value">
              {{ f.label }}
            </button>
          </div>
        </section>

        <!-- 通知方式 -->
        <section id="notify" class="panel scroll-mt-[72px] p-6">
          <h3 class="section-title">通知方式</h3>
          <p class="mb-4 mt-1 text-[12.5px] text-muted">新文献简报的推送渠道（至少一种时可收到提醒）</p>
          <div class="flex flex-col gap-3">
            <div class="flex items-center justify-between rounded-lg border border-line bg-neutral-input/50 px-4 py-3">
              <div>
                <div class="text-[13.5px] font-medium">邮件摘要</div>
                <div class="text-[12px] text-muted">将跟进简报发送到绑定邮箱</div>
              </div>
              <button role="switch" :aria-checked="notify.email"
                      class="relative h-6 w-11 shrink-0 rounded-full transition-colors"
                      :class="notify.email ? 'bg-ok' : 'bg-line'"
                      @click="notify.email = !notify.email">
                <span class="absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all"
                      :class="notify.email ? 'left-[22px]' : 'left-0.5'"></span>
              </button>
            </div>
            <div class="flex items-center justify-between rounded-lg border border-line bg-neutral-input/50 px-4 py-3">
              <div>
                <div class="text-[13.5px] font-medium">浏览器推送</div>
                <div class="text-[12px] text-muted">登录后在浏览器通知中心提醒未读</div>
              </div>
              <button role="switch" :aria-checked="notify.push"
                      class="relative h-6 w-11 shrink-0 rounded-full transition-colors"
                      :class="notify.push ? 'bg-ok' : 'bg-line'"
                      @click="notify.push = !notify.push">
                <span class="absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all"
                      :class="notify.push ? 'left-[22px]' : 'left-0.5'"></span>
              </button>
            </div>
          </div>
        </section>

        <!-- 全局采集来源 -->
        <section id="sources" class="panel scroll-mt-[72px] p-6">
          <h3 class="section-title">全局采集来源</h3>
          <p class="mb-4 mt-1 text-[12.5px] text-muted">作为新建研究方向时的默认来源集合</p>
          <div class="flex flex-wrap gap-2">
            <button v-for="src in ALL_SOURCES" :key="src"
                    class="chip transition-colors"
                    :class="{ 'chip-ok': sources.includes(src) }"
                    @click="toggleSource(src)">
              {{ src }}
            </button>
          </div>
        </section>

        <!-- 保存条 -->
        <div class="panel flex items-center justify-between gap-3 px-6 py-4">
          <span class="text-[12px] text-muted">修改仅在本页生效，需点击保存写入</span>
          <div class="flex items-center gap-3">
            <span v-if="savedMsg" class="text-[12.5px] text-ok">{{ savedMsg }}</span>
            <span v-if="errorMsg" class="text-[12.5px] text-danger">{{ errorMsg }}</span>
            <CapsuleButton :disabled="saving" @click="save">
              {{ saving ? '保存中…' : '保存设置' }}
            </CapsuleButton>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
