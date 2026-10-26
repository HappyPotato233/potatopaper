<script setup lang="ts">
// 登录页：暖米黄学术风双栏（左侧品牌与能力说明，右侧登录表单）
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  Radar, User, Lock, FolderGit2, FileText, MessageSquareText, AlertCircle,
} from 'lucide-vue-next'
import { useAuthStore } from '@/stores/auth'
import CapsuleButton from '@/components/CapsuleButton.vue'

const auth = useAuthStore()
const router = useRouter()

const username = ref('')
const password = ref('')
const loading = ref(false)
const error = ref('')

async function submit() {
  if (!username.value || !password.value) { error.value = '请输入用户名和密码'; return }
  loading.value = true
  error.value = ''
  try {
    await auth.login(username.value, password.value)
    router.push('/')
  } catch (e: any) {
    error.value = e.message || '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="flex min-h-full items-center justify-center bg-background p-6">
    <div class="grid w-full max-w-[980px] overflow-hidden rounded-2xl border border-line bg-card shadow-[0_18px_46px_-32px_rgba(51,51,51,0.45)] md:grid-cols-2">

      <!-- 左：品牌与能力说明（小屏隐藏） -->
      <aside class="hidden flex-col justify-between bg-sidebar p-10 md:flex">
        <div>
          <div class="flex items-center gap-3">
            <span class="flex h-11 w-11 items-center justify-center rounded-xl border border-line bg-card text-primary-ink">
              <Radar :size="22" :stroke-width="1.6" />
            </span>
            <div>
              <div class="brand-logo !text-[21px]">PaperPotato</div>
              <div class="text-[10.5px] tracking-[0.12em] text-muted">RESEARCH RADAR · v2.4</div>
            </div>
          </div>

          <div class="page-eyebrow mt-12">SCHOLARLY LITERATURE RADAR</div>
          <h1 class="mt-2 font-brand text-[27px] font-semibold leading-[1.35]">
            把散落的论文，<br />收进一个安静的雷达。
          </h1>
          <p class="mt-3 text-[12.5px] leading-relaxed text-muted">
            按研究方向自动跟踪最新文献，完成去重、相关度排序与摘要入库，
            需要时再回来追问。
          </p>

          <ul class="mt-8 flex flex-col gap-4">
            <li class="flex items-start gap-3">
              <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-cream/70 text-primary-ink">
                <FolderGit2 :size="15" :stroke-width="1.7" />
              </span>
              <div>
                <div class="text-[13px] font-medium">研究方向自动跟踪</div>
                <div class="text-[11.5px] text-muted">按关键词与来源定时抓取，无需手动翻库</div>
              </div>
            </li>
            <li class="flex items-start gap-3">
              <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-cream/70 text-primary-ink">
                <FileText :size="15" :stroke-width="1.7" />
              </span>
              <div>
                <div class="text-[13px] font-medium">相关度排序与去重</div>
                <div class="text-[11.5px] text-muted">只留下与你课题真正相关的新文献</div>
              </div>
            </li>
            <li class="flex items-start gap-3">
              <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-cream/70 text-primary-ink">
                <MessageSquareText :size="15" :stroke-width="1.7" />
              </span>
              <div>
                <div class="text-[13px] font-medium">随问随答的文献助手</div>
                <div class="text-[11.5px] text-muted">基于已入库文献给出带引用的答案</div>
              </div>
            </li>
          </ul>
        </div>

        <div class="mt-10 border-t border-line pt-4 font-mono text-[10px] tracking-[0.14em] text-muted">
          学术文献雷达 · PAPER POTATO
        </div>
      </aside>

      <!-- 右：登录表单 -->
      <section class="flex flex-col justify-center p-8 md:p-12">
        <!-- 小屏品牌 -->
        <div class="mb-8 flex items-center gap-2.5 md:hidden">
          <Radar :size="22" :stroke-width="1.6" class="text-primary-ink" />
          <span class="brand-logo !text-[19px]">PaperPotato</span>
        </div>

        <div class="page-eyebrow">WELCOME BACK · 登录</div>
        <h2 class="mt-1.5 font-brand text-[26px] font-semibold leading-tight">欢迎回来</h2>
        <p class="mt-1.5 text-[12.5px] text-muted">输入管理员分配的账号，继续你的文献跟踪。</p>

        <form class="mt-7 flex flex-col gap-4" @submit.prevent="submit">
          <label class="block">
            <span class="mb-1.5 block text-[12px] text-muted">用户名</span>
            <span class="relative block">
              <User :size="16" class="absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
              <input v-model="username" class="input !bg-card !pl-9" placeholder="请输入用户名"
                     autocomplete="username" />
            </span>
          </label>

          <label class="block">
            <span class="mb-1.5 block text-[12px] text-muted">密码</span>
            <span class="relative block">
              <Lock :size="16" class="absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
              <input v-model="password" type="password" class="input !bg-card !pl-9" placeholder="请输入密码"
                     autocomplete="current-password" />
            </span>
          </label>

          <div v-if="error" class="flex items-start gap-2 rounded-lg border border-danger/30 bg-[#f3dedd] px-3 py-2 text-[12.5px] text-danger">
            <AlertCircle :size="14" class="mt-0.5 shrink-0" />
            <span>{{ error }}</span>
          </div>

          <CapsuleButton type="submit" class="mt-1 w-full" :disabled="loading">
            {{ loading ? '登录中…' : '登 录' }}
          </CapsuleButton>
        </form>

        <p class="mt-6 border-t border-line pt-4 text-[11.5px] leading-relaxed text-muted">
          账号由管理员统一分配；如需开通或重置密码，请联系所在实验室管理员。
        </p>
      </section>
    </div>
  </div>
</template>
