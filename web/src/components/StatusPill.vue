<script setup lang="ts">
// 状态胶囊：ok / muted / danger / warning / info（工作台、方向、日志共用）
withDefaults(defineProps<{
  tone?: 'ok' | 'muted' | 'danger' | 'warning' | 'info'
}>(), { tone: 'muted' })
</script>

<template>
  <span class="badge" :class="{
          'badge-ok': tone === 'ok',
          'badge-muted': tone === 'muted',
          'badge-danger': tone === 'danger',
          'badge-warning': tone === 'warning',
          'badge-info': tone === 'info',
        }">
    <slot />
  </span>
</template>
