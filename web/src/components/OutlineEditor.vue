<script setup lang="ts">
// 综述可编辑大纲（F-REV-04/05）：编辑/预览双态 + 章节增删/层级切换/上下移/挂引用 + 导出 Word
// 编辑仅前端本地态：不落库、无保存接口，刷新即失效（服务端仅按提交内容生成 docx）
import { ref, watch } from 'vue'
import { ArrowUp, ArrowDown, Trash2, Plus, FileDown, Pencil, Eye, Link2, X } from 'lucide-vue-next'
import { downloadReviewDocx, type OutlineNode, type QaCitation } from '@/api/client'
import CapsuleButton from '@/components/CapsuleButton.vue'

const props = defineProps<{
  title: string
  nodes: OutlineNode[]
  citations: QaCitation[]
}>()

const MAX_DEPTH = 3
const DEPTH_LABELS = ['H1', 'H2', 'H3']

const local = ref<OutlineNode[]>(clone(props.nodes))
const editMode = ref(true)
const exporting = ref(false)
const pickerFor = ref<string | null>(null)   // 当前展开引用选择器的章节 id

let seq = 0

/** 深拷贝：避免直接改动父组件传入的结果对象 */
function clone(nodes: OutlineNode[]): OutlineNode[] {
  return JSON.parse(JSON.stringify(nodes || []))
}

function newId(): string {
  return `x${++seq}${Date.now() % 100000}`
}

// 新综述结果送达 → 重置本地大纲（父组件以 key 重建时同样生效）
watch(() => props.nodes, (v) => { local.value = clone(v); pickerFor.value = null })

/** 上/下移：与相邻章节交换位置 */
function move(i: number, dir: -1 | 1) {
  const j = i + dir
  if (j < 0 || j >= local.value.length) return
  const arr = local.value
  ;[arr[i], arr[j]] = [arr[j], arr[i]]
}

function remove(i: number) {
  local.value.splice(i, 1)
}

/** 在第 i 章之后插入新章节（i = -1 表示追加到末尾） */
function addNode(i: number, depth: number) {
  local.value.splice(i + 1, 0,
    { id: newId(), depth, title: '新章节', content: '', citations: [] })
}

function setDepth(node: OutlineNode, d: number) {
  node.depth = d
}

/** 挂/摘引用：引用序号沿用全局编号（指向 citations[] 下标 +1），重排后仍可溯源 */
function toggleCitation(node: OutlineNode, n: number) {
  const at = node.citations.indexOf(n)
  if (at >= 0) node.citations.splice(at, 1)
  else node.citations.push(n)
  node.citations.sort((a, b) => a - b)
}

async function exportWord() {
  if (exporting.value) return
  exporting.value = true
  try {
    await downloadReviewDocx({
      title: props.title, outline: local.value, citations: props.citations,
    })
  } catch (e: any) {
    alert(`导出失败：${e.message}`)
  } finally {
    exporting.value = false
  }
}
</script>

<template>
  <div>
    <!-- 工具栏：编辑 / 预览 双态切换 + 导出 Word -->
    <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
      <div class="flex items-center gap-1.5">
        <button class="flex items-center gap-1 rounded-lg border px-3 py-1.5 text-[13px] transition-colors"
                :class="editMode ? 'border-primary bg-primary' : 'border-line bg-neutral-input'"
                @click="editMode = true">
          <Pencil :size="13" /> 编辑
        </button>
        <button class="flex items-center gap-1 rounded-lg border px-3 py-1.5 text-[13px] transition-colors"
                :class="!editMode ? 'border-primary bg-primary' : 'border-line bg-neutral-input'"
                @click="editMode = false">
          <Eye :size="13" /> 预览
        </button>
      </div>
      <CapsuleButton :disabled="exporting" @click="exportWord">
        <FileDown :size="14" /> {{ exporting ? '导出中…' : '导出 Word' }}
      </CapsuleButton>
    </div>

    <!-- 编辑态 -->
    <div v-if="editMode" class="space-y-3">
      <div v-for="(node, i) in local" :key="node.id"
           class="rounded-lg border border-line bg-neutral-input/40 p-3"
           :style="{ marginLeft: (node.depth - 1) * 20 + 'px' }">
        <div class="mb-2 flex items-center gap-2">
          <span class="badge badge-muted">{{ i + 1 }}</span>
          <div class="flex items-center gap-1">
            <button v-for="(lb, d) in DEPTH_LABELS" :key="lb"
                    class="rounded border px-1.5 py-0.5 text-[11px] transition-colors"
                    :class="node.depth === d + 1 ? 'border-primary bg-primary' : 'border-line'"
                    @click="setDepth(node, d + 1)">{{ lb }}</button>
          </div>
          <div class="ml-auto flex items-center gap-1">
            <button class="rounded p-1 text-muted transition-colors hover:bg-cream disabled:opacity-30"
                    :disabled="i === 0" title="上移" @click="move(i, -1)"><ArrowUp :size="14" /></button>
            <button class="rounded p-1 text-muted transition-colors hover:bg-cream disabled:opacity-30"
                    :disabled="i === local.length - 1" title="下移" @click="move(i, 1)"><ArrowDown :size="14" /></button>
            <button class="rounded p-1 text-muted transition-colors hover:bg-cream" title="删除" @click="remove(i)">
              <Trash2 :size="14" />
            </button>
          </div>
        </div>
        <input v-model="node.title" class="input mb-2" placeholder="章节标题" />
        <textarea v-model="node.content" class="input min-h-[80px] resize-y text-[13px]" rows="4"
                  placeholder="章节正文（可含 [n] 引用标注）" />
        <!-- 引用：已挂引用 + 挂/摘选择器 -->
        <div class="mt-2 flex flex-wrap items-center gap-1.5">
          <span class="text-[12px] text-muted">引用：</span>
          <span v-for="n in node.citations" :key="n" class="badge badge-primary">
            [{{ n }}] {{ (citations[n - 1]?.title || '').slice(0, 18) }}
            <button class="ml-1 opacity-70 hover:opacity-100" @click="toggleCitation(node, n)"><X :size="10" /></button>
          </span>
          <button class="badge badge-muted border-0 hover:bg-cream"
                  @click="pickerFor = pickerFor === node.id ? null : node.id">
            <Link2 :size="11" /> 挂引用
          </button>
        </div>
        <div v-if="pickerFor === node.id"
             class="mt-2 grid grid-cols-1 gap-1 rounded border border-line bg-cream p-2 sm:grid-cols-2">
          <label v-for="(c, ci) in citations" :key="ci"
                 class="flex cursor-pointer items-start gap-1.5 text-[12px]">
            <input type="checkbox" class="mt-0.5 accent-primary"
                   :checked="node.citations.includes(ci + 1)"
                   @change="toggleCitation(node, ci + 1)" />
            <span class="line-clamp-1">[{{ ci + 1 }}] {{ c.title }}</span>
          </label>
        </div>
        <!-- 章节追加 -->
        <div class="mt-2 flex gap-3">
          <button class="flex items-center gap-0.5 text-[12px] text-muted hover:text-primary-deep"
                  @click="addNode(i, node.depth)">
            <Plus :size="11" /> 同级章节
          </button>
          <button v-if="node.depth < MAX_DEPTH"
                  class="flex items-center gap-0.5 text-[12px] text-muted hover:text-primary-deep"
                  @click="addNode(i, node.depth + 1)">
            <Plus :size="11" /> 子章节
          </button>
        </div>
      </div>
      <div class="flex items-center justify-between">
        <p v-if="local.length === 0" class="text-[13px] text-muted">（暂无章节）</p>
        <button class="flex items-center gap-0.5 text-[12px] text-muted hover:text-primary-deep"
                @click="addNode(local.length - 1, 1)">
          <Plus :size="12" /> 添加章节
        </button>
      </div>
    </div>

    <!-- 预览态：按层级缩进，章节序号按出现顺序自动重算 -->
    <div v-else class="space-y-4">
      <div v-for="(node, i) in local" :key="node.id" :style="{ marginLeft: (node.depth - 1) * 20 + 'px' }">
        <div class="mb-1 font-semibold"
             :class="node.depth === 1 ? 'text-[16px]' : node.depth === 2 ? 'text-[15px]' : 'text-[14px]'">
          {{ i + 1 }}. {{ node.title }}
        </div>
        <div class="whitespace-pre-wrap text-[14px] leading-relaxed">{{ node.content }}</div>
        <div v-if="node.citations.length" class="mt-1 flex flex-wrap gap-1.5">
          <span v-for="n in node.citations" :key="n" class="badge badge-primary">
            [{{ n }}] {{ (citations[n - 1]?.title || '').slice(0, 18) }}
          </span>
        </div>
      </div>
      <p v-if="local.length === 0" class="text-[13px] text-muted">（暂无章节）</p>
    </div>
  </div>
</template>
