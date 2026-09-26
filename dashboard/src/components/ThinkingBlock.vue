<template>
  <el-collapse v-model="activeNames" class="thinking-block">
    <el-collapse-item :name="name">
      <template #title>
        <span class="thinking-block-title">{{ $t('chat.thinking_process') }}</span>
      </template>
      <div ref="thinkingContentRef" v-if="content" class="thinking-block-content markdown-body" v-html="renderedContent"></div>
    </el-collapse-item>
  </el-collapse>
</template>

<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
  name: { type: String, required: true },
  content: { type: String, default: '' },
  renderedContent: { type: String, default: '' }
})
const emit = defineEmits(['update:modelValue'])
const thinkingContentRef = ref(null)

const activeNames = computed({
  get: () => props.modelValue,
  set: value => emit('update:modelValue', value)
})

watch(() => props.content, () => {
  const content = thinkingContentRef.value
  if (content && content.scrollHeight > content.clientHeight) {
    content.scrollTop = content.scrollHeight
  }
}, { flush: 'post' })
</script>

<style scoped lang="scss" src="../assets/css/ThinkingBlock.scss"></style>
