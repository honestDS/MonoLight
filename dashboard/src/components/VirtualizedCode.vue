<template>
  <div class="code-viewer-container">
    <!-- 复制按钮 -->
    <div class="copy-btn-wrapper">
      <el-button 
        title="复制全部代码"
        @click="copyContent"
        class="copy-btn"
      >
        <el-icon><CopyDocument /></el-icon>
      </el-button>
    </div>

    <div class="code-main" :style="{ maxHeight: maxHeight + 'px' }">
      <div class="code-layout">
        <div v-for="(line, index) in visibleLines" :key="index" class="code-line">
          <div class="line-number">{{ index + 1 }}</div>
          <div class="line-content">
            <pre><code>{{ line || ' ' }}</code></pre>
          </div>
        </div>
      </div>
    </div>
    
    <!-- 操作按钮 (悬浮在右下角) -->
    <div v-if="hasMore" class="expand-actions">
      <div class="stats-info">已显示 {{ limit }} / {{ allLines.length }} 行</div>
      <el-button-group>
        <el-button size="small" plain @click="expandMore">继续展开 (+100行)</el-button>
        <el-button size="small" type="primary" @click="expandAll">展开全部</el-button>
      </el-button-group>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
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

const limit = ref(100)

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

const allLines = computed(() => {
  return formattedContent.value.split('\n')
})

const visibleLines = computed(() => {
  return allLines.value.slice(0, limit.value)
})

const visibleText = computed(() => {
  return visibleLines.value.join('\n')
})

const hasMore = computed(() => {
  return allLines.value.length > limit.value
})

const expandMore = () => {
  limit.value += 100
}

const expandAll = () => {
  limit.value = allLines.value.length
}

const reset = () => {
  limit.value = 100
}

const copyContent = async () => {
  const textToCopy = formattedContent.value
  if (!textToCopy) return

  try {
    // 优先使用现代 Clipboard API
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(textToCopy)
      ElMessage.success('复制成功')
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
          ElMessage.success('复制成功')
        } else {
          ElMessage.error('复制失败')
        }
      } catch (err) {
        ElMessage.error('复制失败')
      }
      document.body.removeChild(textArea)
    }
  } catch (err) {
    ElMessage.error('复制失败: ' + err.message)
  }
}

defineExpose({ reset })
</script>

<style lang="scss" scoped>
.code-viewer-container {
  position: relative;
  border: 1px solid #e9ecef;
  border-radius: 6px;

  &:hover {
    .copy-btn-wrapper {
      opacity: 1;
    }
  }

  .copy-btn-wrapper {
    position: absolute;
    top: 12px;
    right: 12px;
    z-index: 20;
    opacity: 0;
    transition: all 0.2s ease-in-out;

    .copy-btn {
      padding: 6px;
      height: 28px;
      width: 28px;
      background-color: rgba(255, 255, 255, 0.7);
      border: 1px solid #e4e7ed;
      border-radius: 0;
      color: #909399;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: all 0.2s;
      
      &:hover {
        background-color: #fff;
        border-color: #409eff;
        color: #409eff;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
      }

      .el-icon {
        font-size: 14px;
      }
    }
  }
  background-color: #f8f9fa;
  width: 100%;
  max-width: 100%;
  overflow: hidden; /* 核心：防止子元素撑开父级宽度 */

  .code-main {
    overflow-y: auto;
    overflow-x: hidden;
    width: 100%;
    
    .code-layout {
      display: flex;
      flex-direction: column;
      width: 100%;
      padding: 8px 0;
    }
  }

  .code-line {
    display: flex;
    width: 100%;
    min-height: 20px;

    &:hover {
      background-color: #f0f0f0;
    }

    .line-number {
      padding: 0 8px;
      background-color: #fafafa;
      border-right: 1px solid #eee;
      text-align: right;
      min-width: 40px;
      user-select: none;
      flex-shrink: 0;
      font-size: 11px;
      color: #999;
      font-family: Consolas, Monaco, monospace;
      line-height: 20px;
    }

    .line-content {
      flex: 1;
      min-width: 0; /* 允许 flex 子项缩小以触发换行 */
      
      pre {
        margin: 0;
        padding: 0 12px;
        font-family: Consolas, Monaco, 'Courier New', monospace;
        font-size: 12px;
        line-height: 20px;
        color: #333;
        white-space: pre-wrap; /* 自动换行 */
        word-break: break-all; /* 强制单词换行，防止长字符串撑开 */
      }
    }
  }

  .expand-actions {
    position: absolute;
    bottom: 8px;
    right: 8px;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 4px;
    z-index: 10;

    .stats-info {
      font-size: 10px;
      color: #909399;
      background: rgba(255, 255, 255, 0.8);
      padding: 2px 6px;
      border-radius: 4px;
      border: 1px solid #ebeef5;
    }

    .el-button-group {
      box-shadow: 0 2px 12px 0 rgba(0,0,0,0.1);
      background: #fff;
      border-radius: 4px;
    }
  }

  /* 滚动条美化 */
  ::-webkit-scrollbar {
    width: 6px;
    height: 6px;
  }
  ::-webkit-scrollbar-thumb {
    background: #ccc;
    border-radius: 3px;
    &:hover {
      background: #bbb;
    }
  }
}
</style>
