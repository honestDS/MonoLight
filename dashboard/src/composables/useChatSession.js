/**
 * 聊天会话管理 composable
 * 封装会话列表和消息管理的相关逻辑
 */
import { ref, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { chatApi } from '../api'
import { useDeleteConfirm } from './useDeleteConfirm'
import { formatTimestamp, isToolCall, isToolResult, getToolName, getToolArguments, getToolResultName, getToolResultContent, getMessageTimestamp } from '../utils'
import { PAGE_SIZE } from '../constants'

export function useChatSession() {
  // 消息相关状态
  const messages = ref([])
  const inputMsg = ref('')
  const loading = ref(false)
  const messageList = ref(null)

  // 会话相关状态
  const sessions = ref([])
  const sessionsLoading = ref(false)
  const currentSessionId = ref(null)

  // 折叠面板状态
  const activeCollapse = ref([])

  // 分页相关
  const currentPage = ref(1)
  const hasMore = ref(true)
  const historyLoading = ref(false)
  let isFirstLoad = true
  let loadedPageCount = 0

  // 加载会话列表
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
    loadSessionHistory(2)
  }

  // 加载会话历史
  const loadSessionHistory = async (pageCount = 1) => {
    if (!currentSessionId.value || historyLoading.value || !hasMore.value) return
    
    historyLoading.value = true
    try {
      const totalSize = PAGE_SIZE * pageCount
      const res = await chatApi.sessionsHistory(currentSessionId.value, 1, totalSize)
      const historyData = res.data?.data || []
      
      if (historyData.length > 0) {
        messages.value = [...historyData, ...messages.value]
        loadedPageCount += pageCount
        if (historyData.length < totalSize) {
          hasMore.value = false
        }
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

  // 滚动到底部
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
    
    if (loadedPageCount >= 2) {
      hasMore.value = false
      return
    }
    
    if (messageList.value.scrollTop < 50) {
      loadSessionHistory(2)
    }
  }

  // 新建会话
  const createNewSession = () => {
    currentSessionId.value = null
    messages.value = []
    inputMsg.value = ''
    ElMessage.info('新会话已创建，请输入消息开始聊天')
  }

  // 发送消息
  const send = async () => {
    if (!inputMsg.value.trim() || loading.value) return
    const text = inputMsg.value
    const userMsgId = Date.now()
    messages.value.push({ id: userMsgId, role: 'user', content: text, created_at: Date.now() / 1000 })
    inputMsg.value = ''
    loading.value = true
    nextTick(() => scrollToBottom())

    const thinkingId = Date.now() + 1
    messages.value.push({ id: thinkingId, role: 'thinking', content: 'Thinking...' })

    try {
      const res = await chatApi.completions({
        message: text,
        session_id: currentSessionId.value || null,
        stream: false
      })
      const aiContent = res.data?.choices?.[0]?.message?.content || '消息解析失败'
      const history = res.data?.history || []
      const aiCreatedAt = res.data?.choices?.[0]?.created_at || null
      
      let isNewSession = false
      if (!currentSessionId.value) {
        isNewSession = true
        await loadSessions()
        if (sessions.value.length > 0) {
          const sortedSessions = [...sessions.value].sort((a, b) => 
            new Date(b.last_active) - new Date(a.last_active)
          )
          currentSessionId.value = sortedSessions[0].session_id
          console.log('新会话创建成功，从列表中获取session_id:', currentSessionId.value)
        }
        await nextTick()
        const activeEl = document.querySelector(`.session-item[data-session-id="${currentSessionId.value}"]`)
        if (activeEl) {
          activeEl.click()
        }
      }

      if (!isNewSession) {
        const thinkingIndex = messages.value.findIndex(m => m.id === thinkingId)
        if (thinkingIndex !== -1) {
          if (history.length > 0) {
            const historyMessages = history.map((item, idx) => ({
              id: `history_${Date.now()}_${idx}`,
              role: item.role,
              content: JSON.stringify(item),
              created_at: item.created_at || null
            }))
            messages.value.splice(thinkingIndex, 1, ...historyMessages)
          } else {
            messages.value.splice(thinkingIndex, 1)
          }
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
        } else {
          console.warn('未找到 Thinking 占位符，尝试清理残留消息')
          const thinkingMsgIndex = messages.value.findIndex(m => m.role === 'thinking')
          if (thinkingMsgIndex !== -1) {
            messages.value.splice(thinkingMsgIndex, 1)
          }
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
        nextTick(() => scrollToBottom())
      }
    } catch (err) {
      const thinkingIndex = messages.value.findIndex(m => m.id === thinkingId)
      if (thinkingIndex !== -1) {
        messages.value.splice(thinkingIndex, 1)
      }
      ElMessage.error(err.message || '发送失败')
    } finally {
      loading.value = false
    }
  }

  // 绑定滚动事件
  const bindScrollEvent = () => {
    if (messageList.value) {
      messageList.value.addEventListener('scroll', handleScroll)
    }
  }

  // 移除滚动事件
  const unbindScrollEvent = () => {
    if (messageList.value) {
      messageList.value.removeEventListener('scroll', handleScroll)
    }
  }

  return {
    // 状态
    messages,
    inputMsg,
    loading,
    messageList,
    sessions,
    sessionsLoading,
    currentSessionId,
    activeCollapse,
    hasMore,
    // 方法
    loadSessions,
    handleDeleteSession,
    selectSession,
    loadSessionHistory,
    scrollToBottom,
    createNewSession,
    send,
    handleScroll,
    bindScrollEvent,
    unbindScrollEvent,
    // 工具函数
    formatTimestamp,
    isToolCall,
    isToolResult,
    getToolName,
    getToolArguments,
    getToolResultName,
    getToolResultContent,
    getMessageTimestamp
  }
}