<script setup lang="ts">
// 文献问答：提问（三检索模式+改写开关）→ 8 节点协作步骤条轮询 → 要点答案+引用（§2.3/§6.3）
// v4：Planner 主-子闭环思考过程面板（规划/工具调用/复审），deep 档迭代轮次徽章，降级回退提示
// 附：方向多视角综述（异步任务，复用 steps/result 轮询；支持 /qa?review=<id> 直达）
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Loader2, Check, X, Circle, Send, BookOpenText, ExternalLink, Minus, ChevronDown } from 'lucide-vue-next'
import { api, type Subscription, type QaStep, type QaResult, type QaSteps,
         type QaToolCall, type QaReviewNotes, type QaCitation } from '@/api/client'
import CapsuleButton from '@/components/CapsuleButton.vue'
import PageHeader from '@/components/PageHeader.vue'
import OutlineEditor from '@/components/OutlineEditor.vue'
import StepThinking from '@/components/StepThinking.vue'

const route = useRoute()
const router = useRouter()

// 全局迭代上限（对齐编排方案 v4 §4.2 MAX_LOOP=3）
const MAX_ITERATION = 3

const PRESETS = [
  'RAG 如何抑制幻觉？',
  '多智能体协作的最新进展有哪些？',
  '推荐几篇关于向量检索优化的论文',
]
const MODES = [
  { value: 'phrase', label: '短语 / 关键词', hint: '直接命中' },
  { value: 'paragraph', label: '段落', hint: '自动拆句并发检索' },
  { value: 'strategy', label: '自定义检索式', hint: '跳过改写' },
]

const query = ref('')
const mode = ref('phrase')
const rewrite = ref(true)
const asking = ref(false)

// 会话态：null=未发起；polling 中更新 steps；完成后填 result
const sessionId = ref('')
const steps = ref<QaStep[]>([])
const complexity = ref<'quick' | 'normal' | 'deep'>('normal')
const result = ref<QaResult | null>(null)
const phase = ref<'idle' | 'running' | 'done' | 'failed'>('idle')
// 思考过程（v4）：迭代轮次 / 工具调用 / 复审意见 / 折叠开关
const loopCounter = ref(0)
const toolCalls = ref<QaToolCall[]>([])
const reviewNotes = ref<QaReviewNotes | null>(null)
const thinkingOpen = ref(false)
let pollTimer: number | null = null

// 编排档位徽章（v4 §4.3 复杂度三档）
const CX_BADGES: Record<string, { label: string; cls: string }> = {
  quick: { label: '快速通道', cls: 'badge badge-muted' },
  normal: { label: '标准编排', cls: 'badge badge-primary' },
  deep: { label: '深度编排', cls: 'badge badge-ok' },
}

// 综述态
const subs = ref<Subscription[]>([])
const reviewSubId = ref<number | null>(null)
const reviewing = ref(false)
const reviewSession = ref('')
const reviewResult = ref<QaResult | null>(null)

// v4 思考过程：主 Agent 规划记录 / 工具调用 / 复审意见
const plannerNotes = computed(() => result.value?.planner_notes ?? [])
const hasThinking = computed(() =>
  plannerNotes.value.length > 0 || toolCalls.value.length > 0 || !!reviewNotes.value)
// 降级判定（v4 §5）：非快速通道但无任何规划记录 → 使用旧编排流水线
const isFallback = computed(() =>
  complexity.value !== 'quick' && !!result.value && plannerNotes.value.length === 0)
// deep 档迭代轮次徽章
const iterationText = computed(() =>
  complexity.value === 'deep' && loopCounter.value > 0
    ? `第 ${loopCounter.value}/${MAX_ITERATION} 轮` : '')
// 综述导出标题（优先取后端回传的方向名，回退当前选中订阅名）
const reviewTitle = computed(() => {
  if (reviewResult.value?.direction_name) return `${reviewResult.value.direction_name}·文献综述`
  const s = subs.value.find(x => x.id === reviewSubId.value)
  return s ? `${s.name}·文献综述` : '文献综述'
})

// ---------- 引用编号渲染 ----------
// 正文里的 [1][2] 渲染成右上角小字蓝色序号：先转义再替换，防止模型输出带入标签
const CITE_RE = /\[(\d{1,2})\]/g
const ESC: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }

function renderPoints(text: string): string {
  const safe = (text || '').replace(/[&<>"']/g, ch => ESC[ch])
  return safe.replace(CITE_RE, (_, n) =>
    `<sup class="mx-0.5 align-super text-[10px] font-semibold text-cite">[${n}]</sup>`)
}

// 点击引用来源：库内文献进内置阅读页，网络来源打开原文
function openCitation(c: QaCitation) {
  if (c.paper_id) router.push(`/papers/${c.paper_id}`)
  else if (c.url) window.open(c.url, '_blank')
}

async function ask(q?: string) {
  const text = (q ?? query.value).trim()
  if (!text || asking.value) return
  asking.value = true
  stopPoll()
  steps.value = []
  result.value = null
  loopCounter.value = 0
  toolCalls.value = []
  reviewNotes.value = null
  thinkingOpen.value = false
  phase.value = 'running'
  try {
    const r = await api.post<{ session_id: string }>('/api/qa/ask', {
      query: text, mode: mode.value, rewrite: rewrite.value,
    })
    if (q) query.value = q
    sessionId.value = r.session_id
    startPoll(sessionId.value, false)
  } catch (e: any) {
    phase.value = 'failed'
    steps.value = [{ key: 'error', label: '提问失败', status: 'failed', detail: e.message }]
  } finally { asking.value = false }
}

// ---------- 轮询（问答与综述共用） ----------
function startPoll(sid: string, isReview: boolean) {
  pollTimer = window.setInterval(async () => {
    try {
      const s = await api.get<QaSteps>(`/api/qa/${sid}/steps`)
      if (isReview) { /* 综述共用 steps 结构，仅用于判断完成 */ }
      else {
        steps.value = s.steps
        complexity.value = s.complexity ?? 'normal'
        loopCounter.value = s.loop_counter ?? 0
        toolCalls.value = s.tool_calls ?? []
        reviewNotes.value = s.review_notes ?? null
      }
      const terminal = (x: QaStep) =>
        x.status === 'done' || x.status === 'failed' || x.status === 'skipped'
      if (s.finished || s.steps.every(terminal)) {
        stopPoll()
        const r = await api.get<QaResult>(`/api/qa/${sid}/result`)
        if (isReview) {
          reviewResult.value = r
          reviewing.value = false
        } else {
          result.value = r
          phase.value = r.error ? 'failed' : 'done'
        }
      }
    } catch {
      stopPoll()
      if (isReview) reviewing.value = false
      else phase.value = 'failed'
    }
  }, 1500)
}

function stopPoll() {
  if (pollTimer !== null) { clearInterval(pollTimer); pollTimer = null }
}

// ---------- 多视角综述 ----------
async function runReview() {
  if (!reviewSubId.value || reviewing.value) return
  reviewing.value = true
  reviewResult.value = null
  try {
    const r = await api.post<{ session_id: string }>('/api/qa/review', {
      subscription_id: reviewSubId.value,
    })
    reviewSession.value = r.session_id
    startPoll(r.session_id, true)
  } catch (e: any) {
    reviewing.value = false
    alert(`综述下发失败：${e.message}`)
  }
}

onMounted(async () => {
  subs.value = await api.get<Subscription[]>('/api/subscriptions')
  // 支持 /qa?review=<id> 直达综述（工作台文献弹窗入口）
  const rid = Number(route.query.review ?? 0)
  if (rid && subs.value.some(s => s.id === rid)) {
    reviewSubId.value = rid
  }
})
onUnmounted(stopPoll)
</script>

<template>
  <div class="page">
    <div class="mx-auto w-full max-w-[980px]">
      <PageHeader eyebrow="Ask · 文献问答"
                  title="文献问答"
                  subtitle="基于你的文献库与网络检索，得到带引用的要点答案" />

      <!-- 提问输入区 -->
      <div class="panel mb-5 p-5">
        <div class="page-eyebrow mb-2">Query</div>
        <textarea v-model="query" class="input mb-4 min-h-[96px] resize-y bg-card" rows="3"
                  placeholder="基于你的文献库与网络检索提问，例如：RAG 如何抑制幻觉？" />
        <div class="mb-4 flex flex-wrap items-center gap-3">
          <div class="segmented">
            <button v-for="m in MODES" :key="m.value"
                    class="segmented-item"
                    :class="{ 'segmented-item-active': mode === m.value }"
                    :title="m.hint" @click="mode = m.value">
              {{ m.label }}
            </button>
          </div>
          <label class="ml-auto flex cursor-pointer items-center gap-1.5 text-[12.5px] text-muted">
            <input v-model="rewrite" type="checkbox" class="accent-primary" /> 查询改写
          </label>
        </div>
        <div class="flex flex-wrap items-end justify-between gap-3 border-t border-line pt-4">
          <div class="flex flex-wrap gap-2">
            <button v-for="p in PRESETS" :key="p" class="chip transition-colors hover:bg-cream"
                    @click="query = p">{{ p }}</button>
          </div>
          <CapsuleButton size="sm" :disabled="asking || !query.trim()" @click="ask()">
            <Send :size="14" /> {{ asking ? '提交中…' : '开始推演' }}
          </CapsuleButton>
        </div>
      </div>

      <!-- 执行区：步骤时间线 -->
      <div v-if="phase !== 'idle' && sessionId" class="panel mb-5 p-5">
        <div class="mb-4 flex items-start justify-between gap-3">
          <div class="min-w-0">
            <div class="page-eyebrow">Pipeline</div>
            <div class="mt-1 truncate text-[14px] font-medium">{{ query }}</div>
          </div>
          <div class="flex shrink-0 items-center gap-2">
            <span v-if="iterationText" class="badge badge-muted">{{ iterationText }}</span>
            <span v-if="CX_BADGES[complexity]" :class="CX_BADGES[complexity].cls">
              {{ CX_BADGES[complexity].label }}
            </span>
          </div>
        </div>
        <div v-if="result?.rewritten" class="mb-4 text-[12.5px] text-muted">
          改写后检索：{{ result.rewritten_query }}
        </div>

        <div>
          <div v-for="(st, idx) in steps" :key="st.key" class="flex gap-3">
            <div class="flex flex-col items-center">
              <div class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border"
                   :class="st.status === 'done' ? 'border-ok bg-softgreen text-ok'
                     : st.status === 'failed' ? 'border-danger bg-[#f3dedd] text-danger'
                     : st.status === 'running' ? 'border-primary-deep bg-cream text-primary-ink'
                     : 'border-line bg-card text-muted'">
                <Loader2 v-if="st.status === 'running'" :size="14" class="animate-spin" />
                <Check v-else-if="st.status === 'done'" :size="14" />
                <X v-else-if="st.status === 'failed'" :size="14" />
                <Minus v-else-if="st.status === 'skipped'" :size="14" />
                <Circle v-else :size="9" />
              </div>
              <div v-if="idx < steps.length - 1" class="my-1 w-px flex-1 bg-line"></div>
            </div>
            <div class="min-w-0 pb-4">
              <div class="text-[13.5px] font-medium"
                   :class="st.status === 'pending' || st.status === 'skipped' ? 'text-muted' : ''">
                {{ st.label }}<span v-if="st.status === 'skipped'" class="ml-1.5 text-[11.5px] font-normal">（跳过）</span>
              </div>
              <div v-if="st.status === 'skipped' && st.detail"
                   class="mt-0.5 text-[12px] leading-relaxed text-muted">{{ st.detail }}</div>
              <StepThinking v-else-if="st.thinking || st.detail"
                            :text="st.thinking || st.detail" :status="st.status" />
            </div>
          </div>
        </div>

        <!-- v4 思考过程（默认收起） -->
        <div class="mt-1 rounded-lg border border-line bg-neutral-input/30">
          <button class="flex w-full items-center justify-between px-4 py-2.5 text-left text-[12.5px] text-muted"
                  @click="thinkingOpen = !thinkingOpen">
            <span>思考过程（Planner 规划 · 工具调用 · 复审结果）</span>
            <ChevronDown :size="14" class="shrink-0 transition-transform"
                         :class="thinkingOpen ? 'rotate-180' : ''" />
          </button>
          <div v-if="thinkingOpen" class="space-y-4 border-t border-line px-4 py-3">
            <div v-if="isFallback" class="text-[12px] text-muted">当前使用旧编排流水线，无主 Agent 思考记录</div>
            <div>
              <div class="mb-1.5 text-[11px] uppercase tracking-wider text-muted">Planner 规划记录</div>
              <div v-if="plannerNotes.length === 0" class="text-[12px] text-muted">—</div>
              <ol v-else class="list-decimal space-y-1 pl-5 text-[12px] leading-relaxed text-muted">
                <li v-for="(n, i) in plannerNotes" :key="i">{{ n }}</li>
              </ol>
            </div>
            <div>
              <div class="mb-1.5 text-[11px] uppercase tracking-wider text-muted">工具调用</div>
              <div v-if="toolCalls.length === 0" class="text-[12px] text-muted">—</div>
              <ul v-else class="space-y-1 text-[12px] text-muted">
                <li v-for="(t, i) in toolCalls" :key="i">
                  <span class="badge badge-primary mr-1.5">{{ t.tool }}</span>{{ t.result_brief }}
                </li>
              </ul>
            </div>
            <div>
              <div class="mb-1.5 text-[11px] uppercase tracking-wider text-muted">复审结果</div>
              <div v-if="!reviewNotes" class="text-[12px] text-muted">
                {{ phase === 'done' ? '未经过完整复审质检' : '待复审' }}
              </div>
              <div v-else class="text-[12px]">
                <span :class="reviewNotes.passed ? 'text-ok' : 'text-danger'">
                  {{ reviewNotes.passed ? '复审通过' : '复审未通过' }}
                </span>
                <ul v-if="reviewNotes.defects.length" class="mt-1 list-disc space-y-0.5 pl-5 text-muted">
                  <li v-for="(d, i) in reviewNotes.defects" :key="i">{{ d }}</li>
                </ul>
                <div v-for="(sg, i) in reviewNotes.suggestions" :key="i" class="mt-1 text-muted">
                  改进指令：{{ sg }}
                </div>
              </div>
            </div>
          </div>
        </div>
        <div v-if="phase === 'running'" class="mt-3 text-[12.5px] text-muted">多步协作执行中，请稍候…</div>
        <div v-if="phase === 'failed'" class="mt-3 rounded-md bg-[#f3dedd] px-3 py-2 text-[12.5px] text-danger">
          {{ result?.error || '问答执行失败，请重试' }}
        </div>
      </div>

      <!-- 结果区：要点答案 + 引用 -->
      <div v-if="phase === 'done' && result" class="panel p-5">
        <div class="mb-4 flex flex-wrap items-center gap-2">
          <span class="page-eyebrow mr-auto">Answer</span>
          <span class="badge badge-muted">迭代 {{ result.iteration_count ?? 1 }} 轮</span>
          <span v-if="reviewNotes" :class="reviewNotes.passed ? 'badge badge-ok' : 'badge badge-danger'">
            {{ reviewNotes.passed ? '复审通过' : '复审未通过' }}
          </span>
          <span v-else class="badge badge-muted">未经过完整复审质检</span>
        </div>

        <h3 class="section-title mb-3">要点答案</h3>
        <ol class="mb-6 list-decimal space-y-2.5 pl-5 text-[14px] leading-relaxed marker:text-primary-ink">
          <li v-for="(p, i) in result.points" :key="i" v-html="renderPoints(p)"></li>
        </ol>

        <h3 class="section-title mb-3">引用来源（{{ result.citations.length }}）</h3>
        <div v-if="result.citations.length === 0" class="text-[12.5px] text-muted">本次未引用外部文献</div>
        <div v-else class="flex flex-col gap-2">
          <button v-for="(c, i) in result.citations" :key="i" type="button"
                  class="flex w-full items-start gap-2 rounded-lg border border-line bg-[#fbf7ea] px-3 py-2.5 text-left transition-colors hover:border-primary-deep"
                  :title="c.paper_id ? '在系统内阅读' : '打开原文'" @click="openCitation(c)">
            <span class="shrink-0 text-[11px] font-semibold leading-snug text-cite">[{{ i + 1 }}]</span>
            <span class="min-w-0 flex-1 text-[12.5px] font-medium leading-snug">{{ c.title }}</span>
            <BookOpenText v-if="c.paper_id" :size="12" class="mt-0.5 shrink-0 text-muted" />
            <ExternalLink v-else :size="12" class="mt-0.5 shrink-0 text-muted" />
          </button>
        </div>
      </div>

      <!-- 多视角综述 -->
      <div class="panel mt-5 p-5">
        <div class="mb-1 flex items-center gap-2">
          <BookOpenText :size="16" class="text-muted" />
          <h3 class="text-[15px] font-semibold">方向多视角综述</h3>
        </div>
        <p class="mb-4 text-[12.5px] text-muted">对选定方向的文献库生成「方法抽取 · 批判审阅 · 横向对比」三视角综述。</p>
        <div class="flex flex-wrap items-center gap-3">
          <select v-model="reviewSubId" class="input max-w-[280px] bg-card">
            <option :value="null" disabled>选择研究方向…</option>
            <option v-for="s in subs" :key="s.id" :value="s.id">{{ s.name }}（{{ s.paper_count }} 篇）</option>
          </select>
          <CapsuleButton size="sm" :disabled="!reviewSubId || reviewing" @click="runReview">
            <Loader2 v-if="reviewing" :size="14" class="animate-spin" />
            {{ reviewing ? '生成中…' : '生成综述' }}
          </CapsuleButton>
          <span v-if="reviewing" class="text-[12px] text-muted">通常需要 1-2 分钟，可离开本页后回来查看结果</span>
        </div>

        <div v-if="reviewResult" class="mt-5">
          <div v-if="reviewResult.error" class="rounded-md bg-[#f3dedd] px-3 py-2 text-[12.5px] text-danger">
            {{ reviewResult.error }}
          </div>
          <template v-else>
            <div class="mb-3 text-[12px] text-muted">
              已分析 {{ reviewResult.paper_count ?? 0 }} 篇文献 · 大纲可编辑并导出 Word（编辑仅本地，刷新失效）
            </div>
            <OutlineEditor :key="reviewSession" :title="reviewTitle"
                           :nodes="reviewResult.outline ?? []" :citations="reviewResult.citations" />
          </template>
        </div>
      </div>
    </div>
  </div>
</template>
