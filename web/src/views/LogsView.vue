<script setup lang="ts">
// 跟进日志：统计行 + 记录表格（状态徽章三态）+ 状态筛选与分页（§2.4）
import { computed, onMounted, ref } from 'vue'
import { ChevronLeft, ChevronRight, ScrollText, Download, PlusCircle } from 'lucide-vue-next'
import { api, type FollowLog } from '@/api/client'
import CapsuleButton from '@/components/CapsuleButton.vue'
import PageHeader from '@/components/PageHeader.vue'
import StatCard from '@/components/StatCard.vue'

const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'success', label: '成功' },
  { value: 'partial_failed', label: '部分失败' },
  { value: 'failed', label: '失败' },
]

// 任务类型展示名（probe = 提交方向前的来源试拉）
const KIND_LABEL: Record<string, string> = {
  manual: '手动', schedule: '调度', login: '登录补发', probe: '试拉',
}

const logs = ref<FollowLog[]>([])
const loading = ref(true)
const status = ref('')
const page = ref(1)
const limit = 20
const total = ref(0)

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / limit)))
// 统计行：累计跟进次数 + 累计拉取/新增（后端聚合，§2.4）
const totalPulled = ref(0)
const totalAdded = ref(0)

function statusBadge(s: string | null): string {
  if (s === 'success') return 'badge badge-ok'
  if (s === 'failed') return 'badge badge-danger'
  return 'badge badge-primary' // partial_failed / 进行中
}

function statusLabel(s: string | null): string {
  if (s === 'success') return '成功'
  if (s === 'failed') return '失败'
  if (s === 'partial_failed') return '部分失败'
  return s ?? '未知'
}

async function load() {
  loading.value = true
  try {
    const data = await api.get<{ total: number; total_pulled: number; total_added: number; items: FollowLog[] }>(
      `/api/logs?page=${page.value}&limit=${limit}&status=${status.value}`)
    total.value = data.total
    totalPulled.value = data.total_pulled
    totalAdded.value = data.total_added
    logs.value = data.items
  } finally { loading.value = false }
}

function filter() { page.value = 1; load() }
function prev() { if (page.value > 1) { page.value--; load() } }
function next() { if (page.value < totalPages.value) { page.value++; load() } }

onMounted(load)
</script>

<template>
  <div class="page">
    <PageHeader eyebrow="Logs · 追踪日志"
                title="跟进日志"
                subtitle="每次手动或调度跟进的拉取、新增与失败明细" />

    <!-- 统计行（累计） -->
    <div class="mb-6 grid grid-cols-1 gap-3.5 sm:grid-cols-3">
      <StatCard label="累计跟进" :value="`${total} 次`" :icon="ScrollText" />
      <StatCard label="累计拉取文献" :value="totalPulled" :icon="Download" />
      <StatCard label="累计新增文献" :value="totalAdded" :icon="PlusCircle" accent="ok" />
    </div>

    <!-- 筛选 -->
    <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
      <div class="segmented">
        <button v-for="o in STATUS_OPTIONS" :key="o.value"
                class="segmented-item"
                :class="{ 'segmented-item-active': status === o.value }"
                @click="status = o.value; filter()">
          {{ o.label }}
        </button>
      </div>
      <span class="text-[12px] text-muted">共 {{ total }} 条</span>
    </div>

    <!-- 表格 -->
    <div v-if="loading" class="panel p-10 text-center text-[13px] text-muted">加载中…</div>
    <div v-else-if="logs.length === 0" class="panel p-10 text-center text-[13px] text-muted">
      暂无跟进记录，可在工作台手动「运行跟进」或等待调度器自动执行
    </div>
    <div v-else class="panel overflow-x-auto">
      <table class="table-editorial">
        <thead>
          <tr>
            <th class="whitespace-nowrap">时间</th>
            <th>方向</th>
            <th>触发</th>
            <th class="text-right">拉取</th>
            <th class="text-right">新增</th>
            <th class="text-right">去重</th>
            <th class="text-right">耗时</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="l in logs" :key="l.id">
            <td class="whitespace-nowrap text-[12px] text-muted">{{ l.started_at ?? '-' }}</td>
            <td class="font-medium">{{ l.direction_name ?? '-' }}</td>
            <td>
              <span class="badge badge-muted">{{ l.kind ? (KIND_LABEL[l.kind] ?? l.kind) : '-' }}</span>
            </td>
            <td class="text-right">{{ l.pulled }}</td>
            <td class="text-right font-medium text-ok">{{ l.added }}</td>
            <td class="text-right text-muted">{{ l.deduped }}</td>
            <td class="whitespace-nowrap text-right text-[12px] text-muted">
              {{ l.duration != null ? `${(l.duration / 1000).toFixed(1)}s` : '-' }}
            </td>
            <td>
              <span :class="statusBadge(l.status)">{{ statusLabel(l.status) }}</span>
              <div v-if="l.source_errors.length" class="mt-1 text-[11.5px] leading-relaxed text-danger">
                {{ l.source_errors.join('；') }}
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 分页 -->
    <div v-if="totalPages > 1" class="mt-4 flex items-center justify-center gap-3">
      <CapsuleButton size="sm" variant="ghost" :disabled="page <= 1" @click="prev"><ChevronLeft :size="13" /></CapsuleButton>
      <span class="text-[12px] text-muted">第 {{ page }} / {{ totalPages }} 页</span>
      <CapsuleButton size="sm" variant="ghost" :disabled="page >= totalPages" @click="next"><ChevronRight :size="13" /></CapsuleButton>
    </div>
  </div>
</template>
