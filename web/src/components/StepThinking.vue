<!-- 单步思考文本：小字号灰色打字机输出，默认保持展开，由用户手动收起 -->
<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { ChevronRight } from 'lucide-vue-next'

const props = defineProps<{ text: string; status: string }>()

const shown = ref('')
const open = ref(false)
const typing = ref(false)
const manual = ref(false)          // 用户手动收起后，不再自动展开
let timer: number | null = null

function stopTyping() {
  if (timer !== null) {
    clearInterval(timer)
    timer = null
  }
  typing.value = false
}

function typeNext() {
  stopTyping()
  if (!props.text) {
    shown.value = ''
    open.value = false
    return
  }
  if (shown.value && !props.text.startsWith(shown.value)) shown.value = ''
  if (!manual.value) open.value = true
  typing.value = true
  // 慢速吐字：50ms 一跳；短文本逐字显示，长文本按比例加速，总时长控制在约 22 秒内
  const charsPerTick = Math.max(1, Math.ceil(props.text.length / 440))
  timer = window.setInterval(() => {
    if (shown.value.length >= props.text.length) {
      stopTyping()
      return
    }
    shown.value = props.text.slice(0, shown.value.length + charsPerTick)
  }, 50)
}

function toggle() {
  manual.value = true
  open.value = !open.value
}

watch(() => props.text, typeNext, { immediate: true })

watch(() => props.status, (status) => {
  if (status === 'running') {
    manual.value = false
    open.value = true
  }
})

onBeforeUnmount(stopTyping)
</script>

<template>
  <div class="mt-1">
    <button v-if="!open" type="button"
            class="inline-flex items-center gap-1 text-[11px] text-muted/70 transition-colors hover:text-muted"
            @click="toggle">
      <ChevronRight :size="11" /> 思考过程
    </button>
    <template v-else>
      <div class="whitespace-pre-wrap break-words text-[11.5px] leading-[1.85] text-muted">
        {{ shown }}<span v-if="typing" class="ml-0.5 animate-pulse">▍</span>
      </div>
      <button v-if="!typing" type="button"
              class="mt-1 text-[11px] text-muted/70 transition-colors hover:text-muted hover:underline"
              @click="toggle">
        收起
      </button>
    </template>
  </div>
</template>
