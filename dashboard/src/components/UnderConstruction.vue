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
.under-construction {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: calc(100vh - 176px);
  margin-bottom: 64px;
  padding: 64px 24px;
  box-sizing: border-box;
  background-color: #ffffff;
  border: 1px solid #e6ebf2;
  border-radius: 8px;
  color: #1f2d3d;
  text-align: center;
}

.under-construction__content {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: min(100%, 560px);
}

.under-construction__icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 88px;
  height: 88px;
  margin-bottom: 24px;
  border-radius: 50%;
  background-color: #eef4fb;
  color: #35658f;
  font-size: 44px;
}

.under-construction__icon :deep(svg) {
  width: 1em;
  height: 1em;
}

.under-construction__status {
  margin: 0 0 12px;
  color: #5b83aa;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.5;
}

.under-construction__title {
  margin: 0;
  color: #1f2d3d;
  font-size: 28px;
  font-weight: 600;
  line-height: 1.35;
}

.under-construction__description {
  margin: 14px 0 0;
  color: #718096;
  font-size: 15px;
  line-height: 1.8;
}

.under-construction__extra {
  width: 100%;
  margin-top: 28px;
}

@media (max-width: 600px) {
  .under-construction {
    min-height: calc(100vh - 144px);
    margin-bottom: 48px;
    padding: 48px 20px;
  }

  .under-construction__icon {
    width: 76px;
    height: 76px;
    margin-bottom: 20px;
    font-size: 38px;
  }

  .under-construction__title {
    font-size: 24px;
  }

  .under-construction__description {
    font-size: 14px;
  }
}
</style>
