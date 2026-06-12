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
    
    <div class="terminal-body" ref="terminalBody">
      <div v-if="logs.length === 0" class="empty-state">
        <el-icon class="is-loading"><Loading /></el-icon>
        <span>{{ $t('realTimeLogs.waiting') }}</span>
      </div>
      
      <!-- 日志项容器：极致扁平化，支持自然文本复制 -->
      <div
        v-for="(log, idx) in filteredLogs"
        :key="idx"
        :class="['log-item', log.level.toLowerCase()]"
      >
        <span class="txt-time">[{{ log.timestamp }}]</span>
        <span class="txt-level">[{{ log.level }}]</span>
        <span class="txt-module">[{{ log.module }}]</span>
        <span class="txt-sep">:</span>
        <span class="txt-message">{{ log.message }}</span>
        
        <!-- 上下文与扩展数据直接衔接在同一个容器流中（通过换行保持格式） -->
        <span v-if="log.uid || log.session_id" class="txt-sub-info">
          {{ $t('realTimeLogs.user_id') }}: {{ log.uid || '-' }} / {{ $t('realTimeLogs.session_id') }}: {{ log.session_id || '-' }}
        </span>
        <span v-if="log.extra && Object.keys(log.extra).length" class="txt-sub-info">
          {{ $t('realTimeLogs.extra_info') }}: {{ formatExtra(log.extra) }}
        </span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, computed, nextTick } from 'vue'
import { Monitor, Delete, Loading } from '@element-plus/icons-vue'
import { systemApi } from '../api'

const logs = ref([])
const isAutoScroll = ref(true)
const terminalBody = ref(null)
let ws = null

const filteredLogs = computed(() => logs.value)

const formatExtra = (extra) => {
  if (!extra) return ''
  try {
    return typeof extra === 'string' ? extra : JSON.stringify(extra)
  } catch {
    return String(extra)
  }
}

const clearLogs = () => {
  logs.value = []
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
        // 历史日志批量下发：一次性追加，避免逐条 push 触发的多次渲染
        const historyLogs = Array.isArray(data.logs) ? data.logs : []
        if (historyLogs.length > 0) {
          logs.value.push(...historyLogs)
          if (logs.value.length > 2000) logs.value.splice(0, logs.value.length - 2000)
          if (isAutoScroll.value) scrollToBottom()
        }
        return
      }

      // 实时推送的单条日志
      logs.value.push(data)
      if (logs.value.length > 2000) logs.value.shift()
      if (isAutoScroll.value) scrollToBottom()
    } catch (err) {
      console.error('WS Error:', err)
    }
  }

  ws.onclose = () => setTimeout(connectWebSocket, 5000)
}

const scrollToBottom = () => {
  nextTick(() => {
    if (terminalBody.value) {
      terminalBody.value.scrollTop = terminalBody.value.scrollHeight
    }
  })
}

onMounted(() => {
  connectWebSocket()
  document.body.classList.add('is-terminal-page')
})

onUnmounted(() => {
  if (ws) ws.close()
  document.body.classList.remove('is-terminal-page')
})
</script>

<style lang="scss">
@import "../assets/css/RealTimeLogs.scss";
</style>
