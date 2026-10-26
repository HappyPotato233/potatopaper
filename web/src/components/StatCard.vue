<script setup lang="ts">
// 统计卡：标签 + 右上角图标 + 衬线大数字 + 可选说明胶囊
import type { Component } from 'vue'

defineProps<{
  label: string
  value: number | string
  hint?: string
  icon?: Component
  accent?: 'default' | 'danger' | 'ok'
}>()
</script>

<template>
  <div class="kpi">
    <div class="flex items-start justify-between gap-3">
      <div class="kpi-label">{{ label }}</div>
      <span v-if="icon" class="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-cream/70 text-primary-ink">
        <component :is="icon" :size="16" :stroke-width="1.7" />
      </span>
    </div>
    <div class="kpi-value mt-3"
         :class="{ '!text-danger': accent === 'danger', '!text-ok': accent === 'ok' }">
      {{ value }}
    </div>
    <div v-if="hint" class="mt-2">
      <span class="chip !py-0.5 !text-[10.5px]"
            :class="accent === 'danger' ? 'chip-danger' : accent === 'ok' ? 'chip-ok' : ''">
        {{ hint }}
      </span>
    </div>
  </div>
</template>
