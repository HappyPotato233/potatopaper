<script setup lang="ts">
// 论文阅读页：单页浏览，底部「已阅读」按钮把该文献标记为已读
// 风格沿用系统暖米黄：panel + page-eyebrow + font-brand + 胶囊按钮
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, CheckCheck, Download, ExternalLink, FileText } from 'lucide-vue-next'
import { api, type Paper } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import CapsuleButton from '@/components/CapsuleButton.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const paper = ref<Paper | null>(null)
const loading = ref(true)
const error = ref('')
const marking = ref(false)

// PDF 原文：后端下载并缓存，前端用 Blob 内嵌展示，支持图片/公式/原始排版
const pdfUrl = ref('')
const pdfLoading = ref(false)
const pdfError = ref('')

async function loadPdf(paperId: number) {
  pdfLoading.value = true
  pdfError.value = ''
  try {
    const blob = await api.getBlob(`/api/papers/${paperId}/pdf`)
    if (pdfUrl.value) URL.revokeObjectURL(pdfUrl.value)
    pdfUrl.value = URL.createObjectURL(blob)
  } catch (e: any) {
    pdfError.value = e.message || 'PDF 原文加载失败'
  } finally {
    pdfLoading.value = false
  }
}

function downloadPdf() {
  if (!pdfUrl.value || !paper.value) return
  const a = document.createElement('a')
  a.href = pdfUrl.value
  a.download = `${paper.value.title.replace(/[\\/:*?"<>|]/g, '_')}.pdf`
  a.click()
}

onBeforeUnmount(() => {
  if (pdfUrl.value) URL.revokeObjectURL(pdfUrl.value)
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    paper.value = await api.get<Paper>(`/api/papers/${route.params.id}`)
    loadPdf(paper.value.id)              // 进入阅读页才下载 PDF，登出后清理
  } catch (e: any) {
    error.value = e.message || '文献加载失败'
  } finally {
    loading.value = false
  }
}

async function markReadDone() {
  if (!paper.value || paper.value.is_read || marking.value) return
  marking.value = true
  try {
    await api.post(`/api/papers/${paper.value.id}/read`)
    paper.value.is_read = true
    await auth.refreshUnread()
  } catch (e: any) {
    error.value = e.message || '标记已读失败'
  } finally {
    marking.value = false
  }
}

function goBack() {
  if (window.history.length > 1) router.back()
  else router.push('/')
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="mx-auto max-w-[820px]">
      <button class="mb-5 inline-flex items-center gap-1.5 text-[12px] text-muted transition-colors hover:text-foreground"
              @click="goBack">
        <ArrowLeft :size="14" /> 返回工作台
      </button>

      <div v-if="loading" class="panel p-12 text-center text-[13px] text-muted">加载中…</div>
      <div v-else-if="error" class="panel p-12 text-center text-[13px] text-danger">{{ error }}</div>

      <article v-else-if="paper" class="panel px-7 py-8 md:px-10">
        <div class="page-eyebrow">PAPER READER</div>
        <h1 class="mt-2 font-brand text-[25px] font-semibold leading-[1.4] md:text-[28px]">{{ paper.title }}</h1>

        <div class="mt-4 flex flex-wrap items-center gap-2 text-[12px] text-muted">
          <span class="chip">[{{ paper.source }}]</span>
          <span v-if="paper.journal">{{ paper.journal }}</span>
          <span v-if="paper.published_at">{{ paper.published_at.slice(0, 10) }}</span>
          <span v-if="paper.journal_level" class="badge badge-primary">{{ paper.journal_level }}</span>
          <span v-if="paper.ccf" class="badge badge-primary">CCF-{{ paper.ccf }}</span>
          <span v-if="paper.cas_zone" class="badge badge-primary">{{ paper.cas_zone }}</span>
          <span v-if="paper.arxiv_cat" class="badge badge-muted">{{ paper.arxiv_cat }}</span>
          <span v-if="paper.is_read" class="chip chip-ok ml-auto">已阅读</span>
        </div>

        <p v-if="paper.authors.length" class="mt-3 text-[12.5px] leading-relaxed text-muted">
          {{ paper.authors.join(', ') }}
        </p>

        <div class="mt-7 border-t border-line pt-6">
          <h2 class="section-title flex items-center gap-2">
            <FileText :size="15" class="text-primary-ink" /> 摘要
          </h2>
          <p class="mt-3 whitespace-pre-line text-[14px] leading-[1.95]">
            {{ paper.abstract || '该文献暂未收录摘要，可通过原文链接查看全文。' }}
          </p>
        </div>

        <div class="mt-7 border-t border-line pt-6">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <h2 class="section-title flex items-center gap-2">
              <FileText :size="15" class="text-primary-ink" /> PDF 原文
            </h2>
            <button v-if="pdfUrl"
                    class="inline-flex items-center gap-1.5 text-[12.5px] font-medium text-primary-ink hover:underline"
                    @click="downloadPdf">
              <Download :size="14" /> 下载原文
            </button>
          </div>
          <div v-if="pdfLoading" class="mt-4 rounded-lg border border-line bg-cream/40 px-4 py-10 text-center">
            <p class="text-[13px] text-muted">正在下载 PDF 原文…</p>
          </div>
          <iframe v-else-if="pdfUrl" :src="pdfUrl" title="论文 PDF 原文"
                  class="mt-3 h-[75vh] w-full rounded-lg border border-line bg-white"></iframe>
          <div v-else class="mt-3 rounded-md bg-cream/70 px-3 py-2 text-[12.5px] leading-relaxed text-muted">
            {{ pdfError || '暂未获取到该文献的 PDF 原文。' }}
            <a v-if="paper.url" :href="paper.url" target="_blank" rel="noopener"
               class="inline-flex items-center gap-1 text-primary-ink hover:underline">
              前往原文 <ExternalLink :size="12" />
            </a>
          </div>
        </div>

        <!-- 滚到最下方：阅读完成操作 -->
        <div class="mt-9 border-t border-line pt-6">
          <CapsuleButton v-if="!paper.is_read" class="w-full" :disabled="marking" @click="markReadDone">
            <CheckCheck :size="15" /> {{ marking ? '标记中…' : '已阅读' }}
          </CapsuleButton>
          <div v-else class="flex items-center justify-center gap-2 rounded-full bg-softgreen py-3 text-[13px] font-medium text-[#3d5c4a]">
            <CheckCheck :size="15" /> 已阅读
          </div>
        </div>
      </article>
    </div>
  </div>
</template>
