<script setup lang="ts">
// 用户管理（admin 专属）：列表 + 搜索分页 + 启停 + 角色调整（§2.6）
import { computed, onMounted, ref } from 'vue'
import { Search, ChevronLeft, ChevronRight, ShieldCheck, UserRound } from 'lucide-vue-next'
import { api, type AdminUser } from '@/api/client'
import CapsuleButton from '@/components/CapsuleButton.vue'
import PageHeader from '@/components/PageHeader.vue'

const users = ref<AdminUser[]>([])
const loading = ref(true)
const q = ref('')
const page = ref(1)
const limit = 20
const total = ref(0)

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / limit)))

async function load() {
  loading.value = true
  try {
    const data = await api.get<{ total: number; items: AdminUser[] }>(
      `/api/admin/users?page=${page.value}&limit=${limit}&q=${encodeURIComponent(q.value)}`)
    total.value = data.total
    users.value = data.items
  } finally { loading.value = false }
}

function search() { page.value = 1; load() }
function prev() { if (page.value > 1) { page.value--; load() } }
function next() { if (page.value < totalPages.value) { page.value++; load() } }

async function toggleStatus(u: AdminUser) {
  const next_status = u.status === 'active' ? 'disabled' : 'active'
  const verb = next_status === 'disabled' ? '停用' : '启用'
  if (!confirm(`确定${verb}用户「${u.username}」？${next_status === 'disabled' ? '停用后将无法登录。' : ''}`)) return
  try {
    await api.patch(`/api/admin/users/${u.id}`, { status: next_status })
    await load()
  } catch (e: any) { alert(e.message) }
}

async function toggleRole(u: AdminUser) {
  const next_role = u.role === 'admin' ? 'user' : 'admin'
  const verb = next_role === 'admin' ? '提升为管理员' : '降级为普通用户'
  if (!confirm(`确定将「${u.username}」${verb}？`)) return
  try {
    await api.patch(`/api/admin/users/${u.id}`, { role: next_role })
    await load()
  } catch (e: any) { alert(e.message) }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <PageHeader eyebrow="Admin · 用户管理"
                title="用户管理"
                subtitle="账号启停与角色升降，仅管理员可见" />

    <!-- 搜索 -->
    <div class="mb-4 flex items-center justify-between gap-4">
      <div class="relative max-w-[280px] flex-1">
        <Search :size="15" class="absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
        <input v-model="q" class="input !pl-9" placeholder="按用户名搜索…"
               @keydown.enter.prevent="search" />
      </div>
      <span class="text-[12px] text-muted">共 {{ total }} 个用户</span>
    </div>

    <!-- 列表 -->
    <div v-if="loading" class="panel p-10 text-center text-[13px] text-muted">加载中…</div>
    <div v-else-if="users.length === 0" class="panel p-10 text-center text-[13px] text-muted">未找到匹配用户</div>
    <div v-else class="panel overflow-x-auto">
      <table class="table-editorial">
        <thead>
          <tr>
            <th>用户名</th>
            <th>角色</th>
            <th>账号状态</th>
            <th>最近登录</th>
            <th>创建时间</th>
            <th class="text-right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="u in users" :key="u.id">
            <td class="font-medium">{{ u.username }}</td>
            <td>
              <span :class="u.role === 'admin' ? 'badge badge-primary' : 'badge badge-muted'">
                {{ u.role === 'admin' ? '管理员' : '用户' }}
              </span>
            </td>
            <td>
              <span :class="u.status === 'active' ? 'badge badge-ok' : 'badge badge-danger'">
                {{ u.status === 'active' ? '启用' : '停用' }}
              </span>
            </td>
            <td class="whitespace-nowrap text-[12.5px] text-muted">{{ u.last_login_at ?? '从未登录' }}</td>
            <td class="whitespace-nowrap text-[12.5px] text-muted">{{ u.created_at ?? '-' }}</td>
            <td>
              <div class="flex justify-end gap-2">
                <CapsuleButton size="sm" variant="ghost" @click="toggleRole(u)">
                  <component :is="u.role === 'admin' ? UserRound : ShieldCheck" :size="13" />
                  {{ u.role === 'admin' ? '降级为用户' : '提升为管理员' }}
                </CapsuleButton>
                <CapsuleButton size="sm" variant="danger-ghost" @click="toggleStatus(u)">
                  {{ u.status === 'active' ? '停用' : '启用' }}
                </CapsuleButton>
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
