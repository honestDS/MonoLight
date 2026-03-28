<template>
  <div class="chat-view-container">
    <!-- 左侧会话列表侧边栏 -->
    <div class="sessions-sidebar">
      <div class="sidebar-header">
        <span>会话列表</span>
        <el-button type="text" size="small" @click="loadSessions">
          <i class="el-icon-refresh"></i>
        </el-button>
      </div>
      <div class="sessions-list">
        <div 
          v-for="session in sessions" 
          :key="session.session_id"
          :data-session-id="session.session_id"
          :class="['session-item', { active: currentSessionId === session.session_id }]"
          @click="selectSession(session)"
        >
          <div class="session-content">
            <div class="session-id">ID: {{ session.session_id }}</div>
            <div class="session-time">{{ session.last_active }}</div>
          </div>
          <div class="session-actions">
            <span class="delete-icon" @click.stop="handleDeleteSession(session.session_id)">删</span>
          </div>
        </div>
        <div v-if="sessions.length === 0 && !sessionsLoading" class="empty-tip">
          暂无会话
        </div>
      </div>
    </div>

    <!-- 右侧聊天区域 -->
    <div class="chat-main">
      <div class="message-list" ref="messageList">
        <div v-if="!currentSessionId && messages.length === 0" class="empty-chat">
          <p>请选择左侧会话或新建会话开始聊天</p>
        </div>
        <template v-else>
          <div v-for="msg in messages" :key="msg.id" :class="['message-item', msg.role === 'user' ? 'user' : msg.role === 'thinking' ? 'thinking' : 'ai']">
            <!-- 普通消息或工具调用消息 -->
            <template v-if="isToolCall(msg)">
              <div class="message-header">
                <span class="message-time">{{ formatTimestamp(getMessageTimestamp(msg)) }}</span>
              </div>
              <el-collapse v-model="activeCollapse">
                <el-collapse-item :name="msg.id">
                  <template #title>
                    <span class="tool-call-title">工具调用: {{ getToolName(msg) }}</span>
                  </template>
                  <div class="tool-call-content">
                    <pre>{{ getToolArguments(msg) }}</pre>
                  </div>
                </el-collapse-item>
              </el-collapse>
            </template>
            <template v-else-if="isToolResult(msg)">
              <div class="message-header">
                <span class="message-time">{{ formatTimestamp(getMessageTimestamp(msg)) }}</span>
              </div>
              <el-collapse v-model="activeCollapse">
                <el-collapse-item :name="msg.id">
                  <template #title>
                    <span class="tool-result-title">工具返回: {{ getToolResultName(msg) }}</span>
                  </template>
                  <div class="tool-result-content">
                    <pre>{{ getToolResultContent(msg) }}</pre>
                  </div>
                </el-collapse-item>
              </el-collapse>
            </template>
            <template v-else>
              <div class="message-header">
                <span class="message-time">{{ formatTimestamp(getMessageTimestamp(msg)) }}</span>
              </div>
              <div class="content">{{ msg.content }}</div>
            </template>
          </div>
        </template>
      </div>
      <div class="input-area">
        <el-button type="primary" @click="createNewSession" class="new-session-btn">
          <i class="el-icon-plus"></i> 新建会话
        </el-button>
        <span v-if="currentSessionId" class="current-session-id">
          当前会话: {{ currentSessionId.substring(0, 8) }}...
        </span>
        <div class="input-wrapper">
          <el-input
            v-model="inputMsg" 
            placeholder="输入消息..." 
            :disabled="loading"
            @keyup.enter="send"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 6 }"
            class="chat-input"
            :resize="'none'"
          />
          <el-button type="primary" :loading="loading" @click="send" :disabled="!inputMsg.trim()">
            发送
          </el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import { ElMessage, ElCollapse, ElCollapseItem } from 'element-plus'
import { chatApi } from '../api'
import { useDeleteConfirm } from '../composables/useDeleteConfirm'

// 格式化时间戳为用户时区的本地时间
const formatTimestamp = (timestamp) => {
  if (!timestamp) return ''
  try {
    const date = new Date(timestamp * 1000)  // 转换为毫秒
    const year = date.getFullYear()
    const month = String(date.getMonth() + 1).padStart(2, '0')
    const day = String(date.getDate()).padStart(2, '0')
    const hours = String(date.getHours()).padStart(2, '0')
    const minutes = String(date.getMinutes()).padStart(2, '0')
    const seconds = String(date.getSeconds()).padStart(2, '0')
    return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`
  } catch {
    return ''
  }
}

const messages = ref([])
const inputMsg = ref('')
const loading = ref(false)
const messageList = ref(null)

// 会话相关
const sessions = ref([])
const sessionsLoading = ref(false)
const currentSessionId = ref(null)

// 折叠面板状态
const activeCollapse = ref([])

// 判断是否为工具调用消息
const isToolCall = (msg) => {
  try {
    const content = msg.content
    if (typeof content === 'string') {
      const parsed = JSON.parse(content)
      return parsed.role === 'assistant' && parsed.tool_calls && parsed.tool_calls.length > 0
    }
    return false
  } catch {
    return false
  }
}

// 获取工具调用名称
const getToolName = (msg) => {
  try {
    const parsed = JSON.parse(msg.content)
    return parsed.tool_calls?.[0]?.name || '未知工具'
  } catch {
    return '未知工具'
  }
}

// 获取工具调用参数
const getToolArguments = (msg) => {
  try {
    const parsed = JSON.parse(msg.content)
    const args = parsed.tool_calls?.[0]?.arguments
    if (typeof args === 'string') {
      return args
    }
    return JSON.stringify(args, null, 2)
  } catch {
    return msg.content
  }
}

// 判断是否为工具返回结果
const isToolResult = (msg) => {
  try {
    const content = msg.content
    if (typeof content === 'string') {
      const parsed = JSON.parse(content)
      return parsed.role === 'tool'
    }
    return false
  } catch {
    return false
  }
}

// 获取工具返回名称
const getToolResultName = (msg) => {
  try {
    const parsed = JSON.parse(msg.content)
    return parsed.tool_call_id ? `ID: ${parsed.tool_call_id.substring(0, 8)}` : '工具返回'
  } catch {
    return '工具返回'
  }
}

// 获取工具返回内容
const getToolResultContent = (msg) => {
  try {
    const parsed = JSON.parse(msg.content)
    return parsed.content || ''
  } catch {
    return msg.content
  }
}

// 获取消息的时间戳（从 created_at 字段或解析的 JSON 中获取）
const getMessageTimestamp = (msg) => {
  // 直接从消息对象获取
  if (msg.created_at) {
    return msg.created_at
  }
  // 从 JSON 解析的内容中获取
  try {
    if (typeof msg.content === 'string') {
      const parsed = JSON.parse(msg.content)
      return parsed.created_at
    }
  } catch {}
  return null
}

// 分页相关
const PAGE_SIZE = 20
const currentPage = ref(1)
const hasMore = ref(true)
const historyLoading = ref(false)
let isFirstLoad = true  // 标记是否为首次加载（用于控制滚动）
let loadedPageCount = 0  // 已加载的页数

// loadSessions 必须在 useDeleteConfirm 之前定义
const loadSessions = async () => {
  sessionsLoading.value = true
  try {
    const res = await chatApi.sessionsList()
    sessions.value = res.data.data || []
  } catch (err) {
    ElMessage.error(err.message || '获取会话列表失败')
  } finally {
    sessionsLoading.value = false
  }
}

// 使用删除确认组合式函数
const { handleDelete: handleDeleteSession } = useDeleteConfirm(chatApi.deleteSession, loadSessions)

// 选择会话
const selectSession = (session) => {
  currentSessionId.value = session.session_id
  messages.value = []
  // 重置分页状态
  currentPage.value = 1
  hasMore.value = true
  historyLoading.value = false
  isFirstLoad = true
  loadedPageCount = 0
  // 加载会话历史
  loadSessionHistory(2)  // 首次加载2页
}

// 加载会话历史
const loadSessionHistory = async (pageCount = 1) => {
  if (!currentSessionId.value || historyLoading.value || !hasMore.value) return
  
  historyLoading.value = true
  try {
    // 每次加载pageCount页，每页PAGE_SIZE条
    const totalSize = PAGE_SIZE * pageCount
    const res = await chatApi.sessionsHistory(currentSessionId.value, 1, totalSize)
    const historyData = res.data?.data || []
    
    if (historyData.length > 0) {
      // 将旧消息unshift到开头（保持时间正序）
      messages.value = [...historyData, ...messages.value]
      
      // 更新已加载页数
      loadedPageCount += pageCount
      
      // 检查是否还有更多数据（数据量小于请求量说明没有更多了）
      if (historyData.length < totalSize) {
        hasMore.value = false
      }
      
      // 首次加载后自动滚动到底部
      if (isFirstLoad) {
        await nextTick()
        scrollToBottom()
        isFirstLoad = false
      }
    } else {
      hasMore.value = false
    }
  } catch (err) {
    ElMessage.error(err.message || '获取历史记录失败')
  } finally {
    historyLoading.value = false
  }
}

// 滚动到底部（平滑滚动）
const scrollToBottom = () => {
  if (messageList.value) {
    messageList.value.scrollTo({
      top: messageList.value.scrollHeight,
      behavior: 'smooth'
    })
  }
}

// 滚动事件处理
const handleScroll = () => {
  if (!messageList.value || !hasMore.value || historyLoading.value) return
  
  // loadedPageCount >= 2 时不再翻页
  if (loadedPageCount >= 2) {
    hasMore.value = false
    return
  }
  
  // 滚动到顶部时加载更多
  if (messageList.value.scrollTop < 50) {
    loadSessionHistory(2)  // 每次加载2页
  }
}

// 新建会话
const createNewSession = () => {
  currentSessionId.value = null
  messages.value = []
  inputMsg.value = ''
  ElMessage.info('新会话已创建，请输入消息开始聊天')
}

const send = async () => {
  if (!inputMsg.value.trim() || loading.value) return
  const text = inputMsg.value
  const userMsgId = Date.now()
  messages.value.push({ id: userMsgId, role: 'user', content: text, created_at: Date.now() / 1000 })
  inputMsg.value = ''
  loading.value = true

  // 发送后平滑滚动到底部
  nextTick(() => scrollToBottom())

  // 添加 Thinking 占位符
  const thinkingId = Date.now() + 1
  messages.value.push({ id: thinkingId, role: 'thinking', content: 'Thinking...' })

  try {
    // 调用 /chat/completions 接口，session_id为空时创建新会话
    const res = await chatApi.completions({
      message: text,
      session_id: currentSessionId.value || null,
      stream: false
    })
    // 解析返回数据 choices[0].message.content (API使用StandardResponse包装)
    const aiContent = res.data?.choices?.[0]?.message?.content || '消息解析失败'
    // 获取 history 数组（LLM后端的工具调用过程）
    const history = res.data?.history || []
    // 获取 AI 回复的时间戳
    const aiCreatedAt = res.data?.choices?.[0]?.created_at || null
    
    // 仅当没有传递 session_id（新会话）时才刷新会话列表
    let isNewSession = false
    if (!currentSessionId.value) {
      isNewSession = true
      // 刷新会话列表
      await loadSessions()
      // 如果是新会话（currentSessionId.value 为 null），找到最新创建的会话
      if (sessions.value.length > 0) {
        // 按 last_active 排序，找到最新的会话
        const sortedSessions = [...sessions.value].sort((a, b) => 
          new Date(b.last_active) - new Date(a.last_active)
        )
        currentSessionId.value = sortedSessions[0].session_id
        console.log('新会话创建成功，从列表中获取session_id:', currentSessionId.value)
      }
      // 模拟点击选中当前会话，会自动加载历史记录（包括 AI 回复）
      await nextTick()
      const activeEl = document.querySelector(`.session-item[data-session-id="${currentSessionId.value}"]`)
      if (activeEl) {
        activeEl.click()
      }
    }
    // 如果不是新会话，手动更新 Thinking 占位符为 AI 回复
    if (!isNewSession) {
      // 更新 Thinking 占位符为实际回复
      const thinkingIndex = messages.value.findIndex(m => m.id === thinkingId)
      if (thinkingIndex !== -1) {
        // 先将 history 中的工具调用消息添加到 messages
        if (history.length > 0) {
          const historyMessages = history.map((item, idx) => ({
            id: `history_${Date.now()}_${idx}`,
            role: item.role,
            content: JSON.stringify(item),
            created_at: item.created_at || null
          }))
          // 在 Thinking 位置插入 history 消息
          messages.value.splice(thinkingIndex, 1, ...historyMessages)
        } else {
          // 没有 history 时，直接移除 Thinking 占位符
          messages.value.splice(thinkingIndex, 1)
        }
        // 尝试解析 AI 返回内容，判断是否为工具调用
        try {
          const parsed = JSON.parse(aiContent)
          if (parsed.role === 'assistant' && parsed.tool_calls && parsed.tool_calls.length > 0) {
            // 工具调用消息
            messages.value.push({ 
              id: thinkingId, 
              role: 'assistant', 
              content: aiContent,  // 保存完整JSON
              created_at: aiCreatedAt
            })
          } else if (parsed.role === 'tool') {
            // 工具返回结果
            messages.value.push({ 
              id: thinkingId, 
              role: 'tool', 
              content: aiContent,  // 保存完整JSON
              created_at: aiCreatedAt
            })
          } else {
            // 普通消息
            messages.value.push({ id: thinkingId, role: 'assistant', content: aiContent, created_at: aiCreatedAt })
          }
        } catch {
          // 不是JSON，普通消息
          messages.value.push({ id: thinkingId, role: 'assistant', content: aiContent, created_at: aiCreatedAt })
        }
      } else {
        // 如果找不到 Thinking 占位符，尝试清理残留的 thinking 消息
        console.warn('未找到 Thinking 占位符，尝试清理残留消息')
        const thinkingMsgIndex = messages.value.findIndex(m => m.role === 'thinking')
        if (thinkingMsgIndex !== -1) {
          messages.value.splice(thinkingMsgIndex, 1)
        }
        // 直接添加 AI 回复
        try {
          const parsed = JSON.parse(aiContent)
          if (parsed.role === 'assistant' && parsed.tool_calls && parsed.tool_calls.length > 0) {
            messages.value.push({ 
              id: thinkingId, 
              role: 'assistant', 
              content: aiContent,
              created_at: aiCreatedAt
            })
          } else if (parsed.role === 'tool') {
            messages.value.push({ 
              id: thinkingId, 
              role: 'tool', 
              content: aiContent,
              created_at: aiCreatedAt
            })
          } else {
            messages.value.push({ id: thinkingId, role: 'assistant', content: aiContent, created_at: aiCreatedAt })
          }
        } catch {
          messages.value.push({ id: thinkingId, role: 'assistant', content: aiContent, created_at: aiCreatedAt })
        }
      }
      // AI回复后平滑滚动到底部
      nextTick(() => scrollToBottom())
    }
  } catch (err) {
    // 移除 Thinking 占位符
    const thinkingIndex = messages.value.findIndex(m => m.id === thinkingId)
    if (thinkingIndex !== -1) {
      messages.value.splice(thinkingIndex, 1)
    }
    ElMessage.error(err.message || '发送失败')
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadSessions()
  // 绑定滚动事件
  if (messageList.value) {
    messageList.value.addEventListener('scroll', handleScroll)
  }
})

// 组件卸载时移除事件监听
onUnmounted(() => {
  if (messageList.value) {
    messageList.value.removeEventListener('scroll', handleScroll)
  }
})
</script>

<style lang="scss">
@import "@/assets/css/chat.scss";
</style>