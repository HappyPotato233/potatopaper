<script setup lang="ts">
// 模型管理（admin 专属）：模型池 CRUD/启停 + Agent 模型绑定（§2.7，F-MODEL-01~05）
import { computed, onMounted, ref } from 'vue'
import { Plus, Pencil, ChevronLeft, ChevronRight, Link2 } from 'lucide-vue-next'
import { api, type ModelItem, type AgentBinding } from '@/api/client'
import CapsuleButton from '@/components/CapsuleButton.vue'
import PageHeader from '@/components/PageHeader.vue'

const VENDORS = ['dashscope', 'volcengine', 'openai', 'deepseek', 'siliconflow', 'ollama', '其他']
const CAPS = ['chat', 'embedding', 'vision']

// ---------- 模型池 ----------
const models = ref<ModelItem[]>([])
const loading = ref(true)
const statusFilter = ref('')
const capFilter = ref('')
const page = ref(1)
const limit = 50
const total = ref(0)
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / limit)))

interface ModelForm {
  id?: number; name: string; vendor: string; model_id: string
  base_url: string; api_key: string; capabilities: string[]; is_default: boolean
}
const form = ref<ModelForm | null>(null)
const saving = ref(false)
const formError = ref('')

async function load() {
  loading.value = true
  try {
    const data = await api.get<{ total: number; items: ModelItem[] }>(
      `/api/admin/models?page=${page.value}&limit=${limit}&status=${statusFilter.value}&capability=${capFilter.value}`)
    total.value = data.total
    models.value = data.items
  } finally { loading.value = false }
}

function filter() { page.value = 1; load() }
function prev() { if (page.value > 1) { page.value--; load() } }
function next() { if (page.value < totalPages.value) { page.value++; load() } }

function openCreate() {
  form.value = { name: '', vendor: 'dashscope', model_id: '', base_url: '', api_key: '',
                 capabilities: ['chat'], is_default: false }
  formError.value = ''
}

function openEdit(m: ModelItem) {
  form.value = { id: m.id, name: m.name, vendor: m.vendor, model_id: m.model_id,
                 base_url: m.base_url, api_key: '', capabilities: [...m.capabilities],
                 is_default: m.is_default }
  formError.value = ''
}

function toggleCap(cap: string) {
  if (!form.value) return
  form.value.capabilities = form.value.capabilities.includes(cap)
    ? form.value.capabilities.filter(x => x !== cap)
    : [...form.value.capabilities, cap]
}

async function save() {
  if (!form.value) return
  if (!form.value.name.trim()) { formError.value = '请填写显示名'; return }
  if (!form.value.model_id.trim()) { formError.value = '请填写 model_id'; return }
  if (form.value.capabilities.length === 0) { formError.value = '至少选择一个能力标签'; return }
  saving.value = true
  formError.value = ''
  try {
    if (form.value.id) {
      // 编辑：api_key 留空 = 保持不变，不提交该字段
      const body: Record<string, unknown> = {
        name: form.value.name.trim(), base_url: form.value.base_url.trim(),
        capabilities: form.value.capabilities, is_default: form.value.is_default,
      }
      if (form.value.api_key.trim()) body.api_key = form.value.api_key.trim()
      await api.put(`/api/admin/models/${form.value.id}`, body)
    } else {
      await api.post('/api/admin/models', {
        name: form.value.name.trim(), vendor: form.value.vendor, model_id: form.value.model_id.trim(),
        base_url: form.value.base_url.trim(), api_key: form.value.api_key.trim(),
        capabilities: form.value.capabilities, is_default: form.value.is_default,
      })
    }
    form.value = null
    await load()
  } catch (e: any) { formError.value = e.message }
  finally { saving.value = false }
}

async function toggleStatus(m: ModelItem) {
  const next_status = m.status === 'active' ? 'disabled' : 'active'
  if (next_status === 'disabled' && m.is_default) {
    if (!confirm(`「${m.name}」是默认模型，停用后将回退到备用默认模型，确定停用？`)) return
  }
  try {
    await api.patch(`/api/admin/models/${m.id}/status`, { status: next_status })
    await load()
  } catch (e: any) { alert(e.message) }
}

// ---------- Agent 绑定 ----------
const agents = ref<AgentBinding[]>([])
const bindError = ref('')

async function loadAgents() {
  agents.value = await api.get<AgentBinding[]>('/api/admin/agents')
}

// 某 agent 的候选模型：启用且能力匹配
function candidatesFor(a: AgentBinding): ModelItem[] {
  return models.value.filter(m => m.status === 'active' && m.capabilities.includes(a.required_capability))
}

async function bind(agentKey: string, modelId: number | null) {
  if (!modelId) return
  bindError.value = ''
  try {
    await api.put(`/api/admin/agents/${agentKey}/model`, { model_id: modelId })
    await loadAgents()
  } catch (e: any) { bindError.value = e.message; await loadAgents() }
}

// 一键切换：把全部能力匹配的 Agent 绑到该模型（能力不匹配的由服务端跳过）
async function bindAll(m: ModelItem) {
  if (!confirm(`把全部 Agent 切到「${m.name}」？能力不匹配的会自动跳过。`)) return
  try {
    const r = await api.put<{ message: string }>(`/api/admin/models/${m.id}/bind-all`)
    alert(r.message)
    await loadAgents()
  } catch (e: any) { alert(e.message) }
}

onMounted(async () => { await load(); await loadAgents() })
</script>

<template>
  <div class="page">
    <PageHeader eyebrow="Admin · 模型管理"
                title="模型管理"
                subtitle="模型池维护与 Agent 绑定，仅管理员可见" />

    <!-- 筛选 + 新增 -->
    <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
      <div class="flex gap-2">
        <select v-model="statusFilter" class="input max-w-[150px]" @change="filter">
          <option value="">全部状态</option>
          <option value="active">启用</option>
          <option value="disabled">停用</option>
        </select>
        <select v-model="capFilter" class="input max-w-[150px]" @change="filter">
          <option value="">全部能力</option>
          <option v-for="c in CAPS" :key="c" :value="c">{{ c }}</option>
        </select>
      </div>
      <CapsuleButton @click="openCreate"><Plus :size="15" /> 新增模型</CapsuleButton>
    </div>

    <!-- 模型池表格 -->
    <div v-if="loading" class="panel p-10 text-center text-[13px] text-muted">加载中…</div>
    <div v-else-if="models.length === 0" class="panel p-10 text-center text-[13px] text-muted">模型池为空，请先新增模型</div>
    <div v-else class="panel overflow-x-auto">
      <table class="table-editorial">
        <thead>
          <tr>
            <th>显示名</th>
            <th>供应商</th>
            <th>model_id</th>
            <th>能力</th>
            <th>默认</th>
            <th>API Key</th>
            <th>状态</th>
            <th>更新时间</th>
            <th class="text-right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="m in models" :key="m.id">
            <td class="font-medium">{{ m.name }}</td>
            <td class="text-[12.5px] text-muted">{{ m.vendor }}</td>
            <td class="font-mono text-[11.5px] text-muted">{{ m.model_id }}</td>
            <td>
              <div class="flex gap-1">
                <span v-for="c in m.capabilities" :key="c" class="badge badge-muted">{{ c }}</span>
              </div>
            </td>
            <td>
              <span v-if="m.is_default" class="badge badge-ok">默认</span>
              <span v-else class="text-[12.5px] text-muted">-</span>
            </td>
            <td>
              <span :class="m.api_key_configured ? 'text-[12.5px] text-ok' : 'text-[12.5px] text-danger'">
                {{ m.api_key_configured ? '已配置' : '未配置' }}
              </span>
            </td>
            <td>
              <button role="switch" :aria-checked="m.status === 'active'"
                      class="relative h-5 w-9 rounded-full transition-colors"
                      :class="m.status === 'active' ? 'bg-ok' : 'bg-line'"
                      :title="m.status === 'active' ? '点击停用' : '点击启用'"
                      @click="toggleStatus(m)">
                <span class="absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all"
                      :class="m.status === 'active' ? 'left-[18px]' : 'left-0.5'"></span>
              </button>
            </td>
            <td class="whitespace-nowrap text-[12.5px] text-muted">{{ m.updated_at ?? '-' }}</td>
            <td class="text-right">
              <CapsuleButton size="sm" variant="ghost" :disabled="m.status !== 'active'"
                             title="把全部 Agent 切到此模型" @click="bindAll(m)">
                <Link2 :size="13" /> 全切
              </CapsuleButton>
              <CapsuleButton size="sm" variant="ghost" title="编辑" @click="openEdit(m)">
                <Pencil :size="13" />
              </CapsuleButton>
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

    <!-- Agent 模型绑定 -->
    <div class="panel mt-8 p-6">
      <div class="mb-1 flex items-center gap-2">
        <Link2 :size="17" class="text-muted" />
        <h3 class="section-title">Agent 模型绑定</h3>
      </div>
      <p class="mb-4 text-[12.5px] text-muted">
        为各 Agent 指定所用模型（候选 = 启用且能力匹配）；切换后对新任务即时生效。未绑定时使用系统默认模型。
      </p>
      <div v-if="bindError" class="mb-3 rounded-md bg-[#f3dedd] px-3 py-2 text-[13px] text-danger">{{ bindError }}</div>
      <div class="flex flex-col gap-3">
        <div v-for="a in agents" :key="a.agent_key"
             class="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-line bg-neutral-input/40 px-4 py-3">
          <div>
            <div class="text-[14px] font-medium">{{ a.label }}</div>
            <div class="text-[12px] text-muted">
              {{ a.agent_key }} · 需要能力 {{ a.required_capability }}
              <template v-if="a.updated_at"> · 最后更新 {{ a.updated_at }}</template>
            </div>
          </div>
          <select class="input max-w-[260px]" :value="a.model_id ?? ''"
                  @change="bind(a.agent_key, Number(($event.target as HTMLSelectElement).value))">
            <option value="" disabled>{{ a.model_name ? a.model_name : '未绑定（用默认模型）' }}</option>
            <option v-for="m in candidatesFor(a)" :key="m.id" :value="m.id">{{ m.name }}（{{ m.vendor }}）</option>
          </select>
        </div>
      </div>
    </div>

    <!-- 新增/编辑弹窗 -->
    <div v-if="form" class="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-6"
         @click.self="form = null">
      <div class="card max-h-[90vh] w-[520px] overflow-y-auto p-6">
        <h3 class="mb-5 text-[16px] font-semibold">{{ form.id ? '编辑模型' : '新增模型' }}</h3>
        <div class="flex flex-col gap-4">
          <div>
            <label class="mb-1.5 block text-[13px] text-muted">显示名（必填）</label>
            <input v-model="form.name" class="input" placeholder="如：qwen3.7-plus" />
          </div>
          <div class="grid grid-cols-2 gap-4">
            <div>
              <label class="mb-1.5 block text-[13px] text-muted">供应商 / 平台</label>
              <select v-model="form.vendor" class="input">
                <option v-for="v in VENDORS" :key="v" :value="v">{{ v }}</option>
              </select>
            </div>
            <div>
              <label class="mb-1.5 block text-[13px] text-muted">model_id（必填，同供应商唯一）</label>
              <input v-model="form.model_id" class="input" :disabled="!!form.id"
                     placeholder="如：qwen3.7-plus" />
            </div>
          </div>
          <div>
            <label class="mb-1.5 block text-[13px] text-muted">base_url（可空 = 官方默认）</label>
            <input v-model="form.base_url" class="input" placeholder="https://..." />
          </div>
          <div>
            <label class="mb-1.5 block text-[13px] text-muted">
              api_key {{ form.id ? '（留空 = 保持不变）' : '（可空）' }}
            </label>
            <input v-model="form.api_key" type="password" class="input" autocomplete="new-password"
                   :placeholder="form.id ? '••••••（不回显）' : '仅提交给服务端'" />
          </div>
          <div>
            <label class="mb-1.5 block text-[13px] text-muted">能力标签（多选）</label>
            <div class="flex gap-2">
              <button v-for="c in CAPS" :key="c"
                      class="rounded-lg border px-3 py-1.5 text-[13px] transition-colors"
                      :class="form.capabilities.includes(c) ? 'border-primary bg-primary' : 'border-line bg-neutral-input'"
                      @click="toggleCap(c)">
                {{ c }}
              </button>
            </div>
          </div>
          <label class="flex cursor-pointer items-center gap-2 text-[14px]">
            <input v-model="form.is_default" type="checkbox" class="accent-primary" />
            设为默认模型（同能力唯一默认）
          </label>

          <div v-if="formError" class="rounded-md bg-[#f3dedd] px-3 py-2 text-[13px] text-danger">{{ formError }}</div>

          <div class="mt-1 flex justify-end gap-2">
            <CapsuleButton variant="ghost" @click="form = null">取消</CapsuleButton>
            <CapsuleButton :disabled="saving" @click="save">
              {{ saving ? '保存中…' : '保存模型' }}
            </CapsuleButton>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
