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

<style scoped lang="scss">
@import "@/assets/css/HelpTooltip.scss";
</style>
