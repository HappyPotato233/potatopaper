<script setup lang="ts">
// 胶囊按钮（前端设计文档 §9.2）
// 规范：大圆角 500px 胶囊、米黄底 #F0E6C8、深棕字 #3E3120、
// hover 时深棕斜切覆盖层从左侧滑入、文字反色为浅色（文字反转）
// 扩展：variant 主/ghost/危险(ghost)；size 常规 md / 紧凑 sm（表格行内按钮、分页按钮等）
withDefaults(defineProps<{
  type?: 'button' | 'submit'
  disabled?: boolean
  variant?: 'primary' | 'ghost' | 'danger' | 'danger-ghost'
  size?: 'md' | 'sm'
}>(), {
  type: 'button', disabled: false, variant: 'primary', size: 'md',
})
</script>

<template>
  <button :type="type" :disabled="disabled" class="capsule-btn"
          :class="[`capsule-${variant}`, `capsule-${size}`]">
    <span class="capsule-label"><slot /></span>
  </button>
</template>

<style scoped>
.capsule-btn {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 10px 24px;
  border: none;
  border-radius: 500px;                 /* §9 大圆角胶囊 */
  background: var(--color-cream);       /* 米黄底 #F0E6C8 */
  color: #3e3120;                       /* 深棕字 */
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  overflow: hidden;                     /* 裁剪斜切覆盖层 */
  white-space: nowrap;
  transition: transform 0.15s ease;
}
.capsule-btn:hover { transform: translateY(-1px); }

/* 深棕斜切覆盖层（默认在左侧外） */
.capsule-btn::before {
  content: '';
  position: absolute;
  inset: 0;
  background: #3e3120;
  transform: skewX(-20deg) translateX(-120%);
  transition: transform 0.3s ease;
}
.capsule-btn:hover::before { transform: skewX(-20deg) translateX(0); }

/* 文字浮于覆盖层之上，hover 反色 */
.capsule-label {
  position: relative;
  z-index: 1;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  transition: color 0.3s ease;
}
.capsule-btn:hover .capsule-label { color: #fffdf6; }

/* disabled：不触发覆盖层与反色 */
.capsule-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
  transform: none;
}
.capsule-btn:disabled::before,
.capsule-btn:disabled:hover::before { transform: skewX(-20deg) translateX(-120%); }
.capsule-btn:disabled .capsule-label,
.capsule-btn:disabled:hover .capsule-label { color: #3e3120; transition: none; }

/* ===== size：紧凑 sm（表格行内 / 分页 / 小操作） ===== */
.capsule-btn.capsule-sm { padding: 4px 12px; font-size: 12px; }

/* ===== variant：ghost（次操作，透明 + 描边） ===== */
.capsule-btn.capsule-ghost {
  background: transparent;
  color: var(--color-foreground, #333333);
  border: 1px solid var(--color-line, #e9e1cc);
}
.capsule-btn.capsule-ghost::before { background: #3e3120; }
.capsule-btn.capsule-ghost:disabled,
.capsule-btn.capsule-ghost:disabled:hover { background: transparent; color: var(--color-foreground, #333333); }

/* ===== variant：danger（危险主操作，保留强调红） ===== */
.capsule-btn.capsule-danger {
  background: var(--color-danger, #c0564f);
  color: #ffffff;
}
.capsule-btn.capsule-danger::before { background: #8f3a34; }
.capsule-btn.capsule-danger .capsule-label,
.capsule-btn.capsule-danger:hover .capsule-label,
.capsule-btn.capsule-danger:disabled .capsule-label,
.capsule-btn.capsule-danger:disabled:hover .capsule-label { color: #ffffff; }

/* ===== variant：danger-ghost（危险次操作，红色描边，hover 填充） ===== */
.capsule-btn.capsule-danger-ghost {
  background: transparent;
  color: var(--color-danger, #c0564f);
  border: 1px solid var(--color-danger, #c0564f);
}
.capsule-btn.capsule-danger-ghost::before { background: var(--color-danger, #c0564f); }
.capsule-btn.capsule-danger-ghost:hover .capsule-label,
.capsule-btn.capsule-danger-ghost:disabled:hover .capsule-label { color: #ffffff; }
.capsule-btn.capsule-danger-ghost .capsule-label,
.capsule-btn.capsule-danger-ghost:disabled .capsule-label { color: var(--color-danger, #c0564f); }
</style>
