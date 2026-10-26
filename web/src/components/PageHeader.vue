<script setup lang="ts">
// 页面页眉：eyebrow + 标题 + 副标题 + 右侧操作插槽（五个业务页共用）
defineProps<{
  eyebrow: string
  title: string
  subtitle?: string
}>()
</script>

<template>
  <header class="mb-6 flex flex-wrap items-end justify-between gap-4">
    <div class="min-w-0">
      <div class="page-eyebrow">{{ eyebrow }}</div>
      <h1 class="mt-1 text-[24px] font-semibold leading-tight">{{ title }}</h1>
      <p v-if="subtitle" class="mt-1 text-[13px] text-muted">{{ subtitle }}</p>
    </div>
    <div v-if="$slots.actions" class="flex items-center gap-2">
      <slot name="actions" />
    </div>
  </header>
</template>
