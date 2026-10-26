<script setup lang="ts">
// 研究方向管理：左侧列表 + 右侧常驻编辑面板（Stitch 设计稿 v4）
// 保留原有流程：新建 / 编辑 / 启停 / 删除 / 批量删除 / 关键词增删 / 多选时间窗校验
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Plus, Play, Pause, Trash2, Search, Clock } from 'lucide-vue-next'
import { api, ALL_SOURCES, type Subscription } from '@/api/client'
import CapsuleButton from '@/components/CapsuleButton.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusPill from '@/components/StatusPill.vue'

const route = useRoute()

const subs = ref<Subscription[]>([])
const loading = ref(true)
const selected = ref<Set<number>>(new Set())

const search = ref('')
const statusTab = ref<'all' | 'active' | 'paused'>('all')

const form = ref<null | { id?: number; name: string; keywords: string[]; sources: string[]; threshold: number; pub_window: string[] }>(null)
const keywordInput = ref('')
const saving = ref(false)
const formError = ref('')

const PUB_WINDOWS = [
  { value: '1Y', label: '1 年内' },
  { value: '1_3Y', label: '1-3 年' },
  { value: '3Y+', label: '3 年以上' },
]

const filtered = computed(() => {
  const kw = search.value.trim().toLowerCase()
  return subs.value.filter(s => {
    if (statusTab.value === 'active' && !s.active) return false
    if (statusTab.value === 'paused' && s.active) return false
    if (!kw) return true
    return s.name.toLowerCase().includes(kw) ||
      s.keywords.some(k => k.toLowerCase().includes(kw))
  })
})

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

async function load() {
  loading.value = true
  try { subs.value = await api.get<Subscription[]>('/api/subscriptions') }
  finally { loading.value = false }
}

function openCreate() {
  form.value = { name: '', keywords: [], sources: ['arXiv'], threshold: 0.5, pub_window: ['1Y'] }
  formError.value = ''
  keywordInput.value = ''
}

function openEdit(s: Subscription) {
  form.value = { id: s.id, name: s.name, keywords: [...s.keywords], sources: [...s.sources],
                 threshold: s.threshold, pub_window: s.pub_window.split(',').filter(Boolean) }
  formError.value = ''
  keywordInput.value = ''
}

function addKeyword() {
  const k = keywordInput.value.trim()
  if (k && form.value && !form.value.keywords.includes(k)) form.value.keywords.push(k)
  keywordInput.value = ''
}

async function save() {
  if (!form.value) return
  if (!form.value.name.trim()) { formError.value = '请填写方向名称'; return }
  if (!form.value.keywords.length) { formError.value = '至少添加一个关键词'; return }
  if (!form.value.pub_window.length) { formError.value = '至少选择一个发表时间范围'; return }
  saving.value = true
  formError.value = ''
  try {
    const body = { name: form.value.name.trim(), keywords: form.value.keywords,
                   sources: form.value.sources, threshold: form.value.threshold,
                   pub_window: form.value.pub_window.join(',') }
    if (form.value.id) await api.put(`/api/subscriptions/${form.value.id}`, body)
    else await api.post('/api/subscriptions', body)
    form.value = null
    await load()
  } catch (e: any) { formError.value = e.message }
  finally { saving.value = false }
}

async function toggleActive(s: Subscription) {
  await api.patch(`/api/subscriptions/${s.id}/active`, { active: !s.active })
  await load()
}

async function remove(s: Subscription) {
  if (!confirm(`确定删除方向「${s.name}」？关联文献与阅读状态将一并删除。`)) return
  await api.delete(`/api/subscriptions/${s.id}`)
  selected.value.delete(s.id)
  if (form.value?.id === s.id) form.value = null
  await load()
}

async function batchDelete() {
  if (!confirm(`确定删除选中的 ${selected.value.size} 个方向？`)) return
  await api.post('/api/subscriptions/batch-delete', { ids: [...selected.value] })
  selected.value.clear()
  await load()
}

function toggleSelect(id: number) {
  if (selected.value.has(id)) selected.value.delete(id)
  else selected.value.add(id)
}

onMounted(async () => {
  const q = route.query.q
  if (typeof q === 'string') search.value = q
  await load()
  if (route.query.new) openCreate()
})
</script>

<template>
  <div class="page">
    <PageHeader eyebrow="Directions · 研究方向"
                title="研究方向管理"
                :subtitle="`共 ${subs.length} 个方向，运行中 ${subs.filter(s => s.active).length} 个`">
      <template #actions>
        <CapsuleButton v-if="selected.size > 0" variant="danger" size="sm" @click="batchDelete">
          <Trash2 :size="14" /> 批量删除（{{ selected.size }}）
        </CapsuleButton>
        <CapsuleButton size="sm" @click="openCreate"><Plus :size="14" /> 新建方向</CapsuleButton>
      </template>
    </PageHeader>

    <!-- 过滤条 -->
    <div class="mb-5 flex flex-wrap items-center gap-3">
      <div class="segmented">
        <button v-for="tab in ([
                  { value: 'all', label: '全部' },
                  { value: 'active', label: '运行中' },
                  { value: 'paused', label: '已暂停' },
                ] as const)" :key="tab.value"
                class="segmented-item"
                :class="{ 'segmented-item-active': statusTab === tab.value }"
                @click="statusTab = tab.value">
          {{ tab.label }}
        </button>
      </div>
      <label class="ml-auto flex h-9 w-[260px] items-center gap-2 rounded-full border border-line bg-card px-3.5">
        <Search :size="14" class="shrink-0 text-muted" />
        <input v-model="search" type="search" placeholder="搜索方向或关键词…"
               class="w-full bg-transparent text-[12.5px] outline-none placeholder:text-muted" />
      </label>
    </div>

    <div class="grid items-start gap-6 lg:grid-cols-12">
      <!-- 左：方向列表 -->
      <section class="flex flex-col gap-3 lg:col-span-7">
        <div v-if="loading" class="panel p-10 text-center text-[13px] text-muted">加载中…</div>
        <div v-else-if="filtered.length === 0" class="panel p-10 text-center text-[13px] text-muted">
          {{ subs.length === 0 ? '暂无方向，点击「新建方向」创建第一个订阅' : '没有匹配的方向' }}
        </div>
        <div v-for="s in filtered" :key="s.id"
             class="panel p-4 transition-shadow hover:shadow-md"
             :class="form?.id === s.id ? 'ring-1 ring-primary-deep' : ''">
          <div class="flex items-start gap-3">
            <input type="checkbox" class="mt-1.5 accent-primary" :checked="selected.has(s.id)"
                   @click.stop="toggleSelect(s.id)" />
            <button class="min-w-0 flex-1 text-left" @click="openEdit(s)">
              <div class="mb-2 flex items-start justify-between gap-3">
                <span class="truncate text-[15px] font-semibold leading-snug">{{ s.name }}</span>
                <StatusPill :tone="s.active ? 'ok' : 'muted'">{{ s.active ? '运行中' : '已暂停' }}</StatusPill>
              </div>
              <div class="mb-2.5 flex flex-wrap gap-1.5">
                <span v-for="k in s.keywords" :key="k" class="chip">{{ k }}</span>
              </div>
              <div class="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11.5px] text-muted">
                <span>{{ s.sources.join(' · ') }}</span>
                <span>阈值 {{ (s.threshold * 100).toFixed(0) }}%</span>
                <span>文献 {{ s.paper_count }} / 未读
                  <b :class="s.unread_count > 0 ? 'text-danger' : ''">{{ s.unread_count }}</b>
                </span>
                <span class="ml-auto flex items-center gap-1">
                  <Clock :size="11" /> {{ relTime(s.last_run_at) }}
                </span>
              </div>
            </button>
            <div class="flex shrink-0 flex-col gap-1">
              <button class="rounded-md p-1.5 text-muted transition-colors hover:bg-cream hover:text-foreground"
                      :title="s.active ? '暂停' : '启动'" @click.stop="toggleActive(s)">
                <component :is="s.active ? Pause : Play" :size="14" />
              </button>
              <button class="rounded-md p-1.5 text-muted transition-colors hover:bg-[#f3dedd] hover:text-danger"
                      title="删除" @click.stop="remove(s)">
                <Trash2 :size="14" />
              </button>
            </div>
          </div>
        </div>
      </section>

      <!-- 右：常驻编辑面板 -->
      <aside class="lg:col-span-5">
        <div class="panel p-5 lg:sticky lg:top-4">
          <div v-if="!form" class="py-14 text-center">
            <div class="page-eyebrow">Inspector</div>
            <p class="mt-2 text-[13px] text-muted">从左侧选择一个方向进行编辑</p>
            <CapsuleButton size="sm" variant="ghost" class="mt-4" @click="openCreate">
              <Plus :size="13" /> 新建方向
            </CapsuleButton>
          </div>

          <template v-else>
            <div class="mb-4 flex items-start justify-between">
              <div>
                <div class="page-eyebrow">{{ form.id ? 'Edit Direction' : 'New Direction' }}</div>
                <h3 class="mt-1 text-[16px] font-semibold">{{ form.id ? '编辑方向' : '新建方向' }}</h3>
              </div>
              <button class="rounded-md px-2 py-1 text-muted hover:bg-cream hover:text-foreground"
                      @click="form = null">✕</button>
            </div>

            <div class="flex flex-col gap-4">
              <div>
                <label class="mb-1.5 block text-[12.5px] text-muted">方向名称</label>
                <input v-model="form.name" class="input" placeholder="如：检索增强生成" />
              </div>

              <div>
                <label class="mb-1.5 block text-[12.5px] text-muted">关键词（回车添加，点击移除）</label>
                <div v-if="form.keywords.length" class="mb-2 flex flex-wrap gap-1.5">
                  <span v-for="k in form.keywords" :key="k"
                        class="chip chip-ok cursor-pointer"
                        @click="form.keywords = form.keywords.filter(x => x !== k)">
                    {{ k }} ✕
                  </span>
                </div>
                <input v-model="keywordInput" class="input" placeholder="输入关键词后按回车"
                       @keydown.enter.prevent="addKeyword" />
              </div>

              <div>
                <label class="mb-1.5 block text-[12.5px] text-muted">采集来源（多选）</label>
                <div class="flex flex-wrap gap-2">
                  <button v-for="src in ALL_SOURCES" :key="src"
                          class="rounded-lg border px-3 py-1.5 text-[12.5px] transition-colors"
                          :class="form.sources.includes(src) ? 'border-primary-deep bg-primary text-primary-ink' : 'border-line bg-neutral-input text-muted'"
                          @click="form.sources = form.sources.includes(src) ? form.sources.filter(x => x !== src) : [...form.sources, src]">
                    {{ src }}
                  </button>
                </div>
              </div>

              <div>
                <label class="mb-1.5 block text-[12.5px] text-muted">
                  相关度阈值：{{ (form.threshold * 100).toFixed(0) }}%
                </label>
                <input v-model.number="form.threshold" type="range" min="0" max="1" step="0.01"
                       class="w-full accent-primary" />
              </div>

              <div>
                <label class="mb-1.5 block text-[12.5px] text-muted">发表时间窗（可多选）</label>
                <div class="flex flex-wrap gap-4">
                  <label v-for="w in PUB_WINDOWS" :key="w.value"
                         class="flex cursor-pointer items-center gap-1.5 text-[12.5px]">
                    <input v-model="form.pub_window" type="checkbox" :value="w.value" class="accent-primary" />
                    {{ w.label }}
                  </label>
                </div>
                <p class="mt-1 text-[11.5px] text-muted">勾选多个表示取并集，如同时选「1 年内」和「3 年以上」表示跳过 1-3 年</p>
              </div>

              <div v-if="formError" class="whitespace-pre-line rounded-md bg-[#f3dedd] px-3 py-2 text-[12.5px] text-danger">
                {{ formError }}
              </div>

              <div class="mt-1 flex justify-end gap-2 border-t border-line pt-4">
                <CapsuleButton variant="ghost" size="sm" @click="form = null">取消</CapsuleButton>
                <CapsuleButton size="sm" :disabled="saving" @click="save">
                  {{ saving ? '检测文献源中…' : '保存方向' }}
                </CapsuleButton>
              </div>
            </div>
          </template>
        </div>
      </aside>
    </div>
  </div>
</template>
