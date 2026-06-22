<template>
  <div class="terminal-view">
    <div class="terminal-toolbar">
      <div class="toolbar-left">
        <span class="terminal-tab">
          <el-icon><Monitor /></el-icon>
          {{ $t('realTimeLogs.realtime_logs') }}
        </span>
      </div>
      <div class="toolbar-right">
        <div class="control-item">
          <span class="label">{{ $t('realTimeLogs.auto_scroll') }}</span>
          <el-switch v-model="isAutoScroll" size="small" />
        </div>
        <el-divider direction="vertical" />
        <el-button
          type="text"
          @click="clearLogs"
          class="toolbar-btn delete"
          :title="$t('realTimeLogs.clear_title')"
        >
          <el-icon><Delete /></el-icon>
          <span>{{ $t('realTimeLogs.clear') }}</span>
        </el-button>
      </div>
    </div>

    <div class="terminal-body">
      <div v-if="logs.length === 0" class="empty-state">
        <el-icon class="is-loading"><Loading /></el-icon>
        <span>{{ $t('realTimeLogs.waiting') }}</span>
      </div>

      <!-- 日志项容器：极致扁平化，支持自然文本复制 -->
      <div
        v-for="log in logs"
        :key="log._id"
        :class="['log-item', log.level.toLowerCase()]"
      >
        <span class="txt-time">[{{ log.timestamp }}]</span>
        <span class="txt-level">[{{ log.level }}]</span>
        <span class="txt-module">[{{ log.module }}]</span>
        <span class="txt-sep">:</span>
        <span class="txt-message">{{ log.message }}</span>

        <!-- 上下文与扩展数据直接衔接在同一个容器流中（通过换行保持格式） -->
        <span v-if="log.uid || log.session_id" class="txt-sub-info">
          [{{ $t('realTimeLogs.user_id') }}: {{ log.uid || '-' }} / {{ $t('realTimeLogs.session_id') }}: {{ log.session_id || '-' }}]
        </span>
        <span v-if="log._extraText" class="txt-sub-info">
          [{{ $t('realTimeLogs.extra_info') }}: {{ log._extraText }}]
        </span>
      </div>

      <!-- 滚动锚点：始终位于日志列表最末尾 -->
      <div ref="scrollAnchor" class="scroll-anchor"></div>
    </div>
  </div>
</template>

<script setup>
import { ref, shallowRef, triggerRef, onMounted, onUnmounted, nextTick } from 'vue'
import { Monitor, Delete, Loading } from '@element-plus/icons-vue'
import { systemApi } from '../api'

const MAX_LOGS = 2000
const FLUSH_INTERVAL = 100

const logs = shallowRef([])
const isAutoScroll = ref(true)
const scrollAnchor = ref(null)
let ws = null
let logIdCounter = 0
let isUnmounted = false

let pendingLogs = []
let flushTimer = null

let scrollPending = false

const prepareLog = (log) => {
  log._id = ++logIdCounter
  const extra = log.extra
  if (extra && typeof extra === 'object') {
    const keys = Object.keys(extra)
    if (keys.length > 0) {
      try {
        log._extraText = JSON.stringify(extra)
      } catch {
        log._extraText = String(extra)
      }
    } else {
      log._extraText = ''
    }
  } else if (typeof extra === 'string' && extra) {
    log._extraText = extra
  } else {
    log._extraText = ''
  }
  return log
}

const scrollToBottom = () => {
  if (scrollPending) return
  scrollPending = true
  // nextTick 确保 DOM 更新完成，requestAnimationFrame 确保浏览器完成布局后再滚动
  // 使用 scrollIntoView 滚动到锚点元素，比直接设置 scrollTop 更可靠
  nextTick(() => {
    requestAnimationFrame(() => {
      scrollPending = false
      if (scrollAnchor.value) {
        scrollAnchor.value.scrollIntoView({ block: 'nearest', behavior: 'auto' })
      }
    })
  })
}

const flushPendingLogs = () => {
  if (pendingLogs.length === 0) return

  const list = logs.value
  list.push(...pendingLogs)
  if (list.length > MAX_LOGS) {
    list.splice(0, list.length - MAX_LOGS)
  }
  pendingLogs = []
  triggerRef(logs)

  if (isAutoScroll.value) scrollToBottom()
}

const clearLogs = () => {
  logs.value = []
  pendingLogs = []
  if (flushTimer) {
    clearTimeout(flushTimer)
    flushTimer = null
  }
}

const connectWebSocket = () => {
  const token = localStorage.getItem('token')
  if (!token) return

  clearLogs()
  ws = systemApi.createLogsWebSocket(token)
  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      if (!data) return

      if (data.type === 'history') {
        const historyLogs = Array.isArray(data.logs) ? data.logs : []
        if (historyLogs.length > 0) {
          historyLogs.forEach(prepareLog)
          const list = logs.value
          list.push(...historyLogs)
          if (list.length > MAX_LOGS) {
            list.splice(0, list.length - MAX_LOGS)
          }
          triggerRef(logs)
          if (isAutoScroll.value) scrollToBottom()
        }
        return
      }

      prepareLog(data)
      pendingLogs.push(data)

      if (!flushTimer) {
        flushTimer = setTimeout(() => {
          flushTimer = null
          flushPendingLogs()
        }, FLUSH_INTERVAL)
      }
    } catch (err) {
      console.error('WS Error:', err)
    }
  }

  ws.onclose = () => {
    if (!isUnmounted) setTimeout(connectWebSocket, 5000)
  }
}

onMounted(() => {
  connectWebSocket()
  document.body.classList.add('is-terminal-page')
})

onUnmounted(() => {
  isUnmounted = true
  if (ws) ws.close()
  if (flushTimer) clearTimeout(flushTimer)
  document.body.classList.remove('is-terminal-page')
})
</script>

<style lang="scss">
@import "../assets/css/RealTimeLogs.scss";
</style>
