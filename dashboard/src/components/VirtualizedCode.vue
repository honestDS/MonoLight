<template>
  <div class="code-viewer-container">
    <!-- 复制按钮 -->
    <div class="copy-btn-wrapper">
      <el-button 
        :title="$t('common.code.copy_all')"
        @click="copyContent"
        class="copy-btn"
      >
        <el-icon><CopyDocument /></el-icon>
      </el-button>
    </div>

    <div class="code-main" :style="{ maxHeight: maxHeight + 'px' }">
      <div class="code-layout">
        <div v-for="(segment, index) in visibleSegments" :key="index" class="code-line">
          <div class="line-number">{{ segment.isContinuation ? '' : segment.sourceLineNumber }}</div>
          <div class="line-content">
            <pre><code>{{ segment.text || ' ' }}</code></pre>
          </div>
        </div>
      </div>
    </div>
    
    <!-- 操作按钮 (悬浮在右下角) -->
    <div v-if="hasMore" class="expand-actions">
      <div class="stats-info">{{ $t('common.code.showing', { limit: limit, total: displaySegments.length }) }}</div>
      <el-button-group>
        <el-button size="small" plain @click="expandMore">{{ $t('common.code.expand_more') }}</el-button>
        <el-button size="small" type="primary" @click="expandAll">{{ $t('common.code.expand_all') }}</el-button>
      </el-button-group>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { CopyDocument } from '@element-plus/icons-vue'

const props = defineProps({
  content: {
    type: String,
    default: ''
  },
  maxHeight: {
    type: Number,
    default: 400
  }
})

const { t } = useI18n()

const PAGE_SIZE = 100
const MAX_CHARS_PER_DISPLAY_LINE = 160

const limit = ref(PAGE_SIZE)

// 尝试格式化 JSON
const formattedContent = computed(() => {
  if (!props.content) return ''
  try {
    const obj = JSON.parse(props.content)
    if (typeof obj === 'object' && obj !== null) {
      return JSON.stringify(obj, null, 2)
    }
  } catch (e) {
    // ignore
  }
  return props.content
})

const sourceLines = computed(() => {
  return formattedContent.value.split('\n')
})

const displaySegments = computed(() => {
  return sourceLines.value.flatMap((line, lineIndex) => {
    if (line.length <= MAX_CHARS_PER_DISPLAY_LINE) {
      return [{
        text: line,
        sourceLineNumber: lineIndex + 1,
        isContinuation: false
      }]
    }

    const segments = []
    for (let start = 0; start < line.length; start += MAX_CHARS_PER_DISPLAY_LINE) {
      segments.push({
        text: line.slice(start, start + MAX_CHARS_PER_DISPLAY_LINE),
        sourceLineNumber: lineIndex + 1,
        isContinuation: start > 0
      })
    }
    return segments
  })
})

const visibleSegments = computed(() => {
  return displaySegments.value.slice(0, limit.value)
})

const hasMore = computed(() => {
  return displaySegments.value.length > limit.value
})

const expandMore = () => {
  limit.value += PAGE_SIZE
}

const expandAll = () => {
  limit.value = displaySegments.value.length
}

const reset = () => {
  limit.value = PAGE_SIZE
}

const copyContent = async () => {
  const textToCopy = formattedContent.value
  if (!textToCopy) return

  try {
    // 优先使用现代 Clipboard API
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(textToCopy)
      ElMessage.success(t('common.code.copy_success'))
    } else {
      // 降级方案：兼容纯 IP/HTTP 等非安全上下文
      const textArea = document.createElement('textarea')
      textArea.value = textToCopy
      // 防止页面滚动
      textArea.style.position = 'fixed'
      textArea.style.left = '-999999px'
      textArea.style.top = '-999999px'
      document.body.appendChild(textArea)
      textArea.focus()
      textArea.select()
      try {
        const successful = document.execCommand('copy')
        if (successful) {
          ElMessage.success(t('common.code.copy_success'))
        } else {
          ElMessage.error(t('common.code.copy_failed'))
        }
      } catch (err) {
        ElMessage.error(t('common.code.copy_failed'))
      }
      document.body.removeChild(textArea)
    }
  } catch (err) {
    ElMessage.error(t('common.code.copy_failed') + ': ' + err.message)
  }
}

defineExpose({ reset })
</script>

<style lang="scss" scoped>
@import "@/assets/css/VirtualizedCode.scss";
</style>
