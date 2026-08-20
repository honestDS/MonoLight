<script setup>
import { Tools } from '@element-plus/icons-vue'

defineProps({
  title: {
    type: String,
    required: true,
  },
  description: {
    type: String,
    required: true,
  },
  statusText: {
    type: String,
    default: '',
  },
})
</script>

<template>
  <section class="under-construction">
    <div class="under-construction__content">
      <div class="under-construction__icon" aria-hidden="true">
        <slot name="icon">
          <Tools />
        </slot>
      </div>

      <p v-if="statusText" class="under-construction__status">{{ statusText }}</p>
      <h1 class="under-construction__title">{{ title }}</h1>
      <p class="under-construction__description">{{ description }}</p>

      <div v-if="$slots.default" class="under-construction__extra">
        <slot />
      </div>
    </div>
  </section>
</template>

<style scoped lang="scss">
@import "@/assets/css/UnderConstruction.scss";
</style>
