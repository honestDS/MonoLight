<script setup>
import helpIcon from '@/assets/svg/help.svg'

const props = defineProps({
  content: {
    type: String,
    default: '',
  },
  ariaLabel: {
    type: String,
    default: '',
  },
})
</script>

<template>
  <el-tooltip placement="top" effect="dark" :show-after="200">
    <template #content>
      <div class="help-tooltip__content">
        <slot>{{ props.content }}</slot>
      </div>
    </template>
    <span
      class="help-tooltip__trigger"
      tabindex="0"
      role="img"
      :aria-label="props.ariaLabel || props.content"
    >
      <img class="help-tooltip__icon" :src="helpIcon" alt="" aria-hidden="true" />
    </span>
  </el-tooltip>
</template>

<style scoped>
.help-tooltip__trigger {
  display: inline-flex;
  align-items: center;
  margin-left: 4px;
  margin-right: 8px;
  cursor: help;
  opacity: 0.72;
  vertical-align: -0.125em;
  align-self: center;
  line-height: 1;
}

.help-tooltip__trigger:hover,
.help-tooltip__trigger:focus-visible {
  opacity: 1;
}

.help-tooltip__trigger:focus-visible {
  outline: 1px solid var(--el-color-primary);
  outline-offset: 2px;
}

.help-tooltip__icon {
  display: block;
  width: 14px;
  height: 14px;
}

.help-tooltip__content {
  max-width: 360px;
  white-space: normal;
  overflow-wrap: anywhere;
  line-height: 1.5;
}
</style>
