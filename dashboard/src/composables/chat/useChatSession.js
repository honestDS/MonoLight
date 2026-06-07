/**
 * 聊天会话管理 composable
 * 组合消息状态、会话管理、通信层、消息处理等模块
 * 保持与原有 API 兼容
 */
import { nextTick, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useChatState } from './useChatState'
import { useSessionManager } from './useSessionManager'
import { useChatTransport } from './useChatTransport'
import { useMessageProcessor } from './useMessageProcessor'
import { formatTimestamp, isToolCall, isToolResult, getToolName, getToolArguments, getToolResultName, getToolResultContent, getMessageTimestamp } from '../../utils'
import { chatApi } from '../../api'

export function useChatSession() {
  // ==================== 组合各模块 ====================

  // 1. 消息状态
  const chatState = useChatState()
  
  // 新增附件状态
  const attachments = ref([])
  
  // 默认 Markdown 开关状态（用于未选择会话时）
  const enableMarkdownDefault = ref(false)
  
  // 2. 会话管理
  const sessionManager = useSessionManager()
  
  // 3. 通信层
  const transport = useChatTransport()
  
  // 4. 消息处理
  const messageProcessor = useMessageProcessor()

  // ==================== 设置模块间连接 ====================
  
  // 设置会话管理的历史记录加载回调
  sessionManager.setLoadHistoryCallback(async (pageCount) => {
    const historyData = await sessionManager.loadSessionHistory(pageCount)
    if (historyData && historyData.length > 0) {
      // 插入到消息列表开头
      chatState.insertMessage(0, historyData, true)
      await nextTick()
      chatState.scrollToBottom()
    }
  })

  // ==================== 核心发送方法 ====================
  
  /**
   * 发送消息（统一入口）
   */
  const send = async () => {
    if (transport.transportMode.value === 'ws') {
      return wsSend()
    } else {
      return httpSend()
    }
  }

  /**
   * HTTP 方式发送消息
   */
  const httpSend = async () => {
    if (!chatState.inputMsg.value.trim() && attachments.value.length === 0) return
    
    const text = chatState.inputMsg.value
    const userMsgId = Date.now()
    
    // 统一渲染顺序：清理之前的 thinking 占位，确保 AI 响应紧跟最新消息（与流式行为一致）
    messageProcessor.cleanupThinkingMessage(chatState.messages)
    
    // 添加用户消息
    const attachmentsToSent = attachments.value.map(a => a.path)
    chatState.addMessage({
      id: userMsgId,
      role: 'user',
      content: text,
      attachments: attachmentsToSent,
      created_at: Date.now() / 1000
    })
    
    chatState.inputMsg.value = ''
    attachments.value = []
    chatState.loading.value = true
    nextTick(() => chatState.scrollToBottom())

    const thinkingId = userMsgId + 1
    chatState.addMessage({ id: thinkingId, role: 'thinking', content: 'Thinking...' })

    await performHttpSend(text, thinkingId, attachmentsToSent)
  }

  /**
   * 实际执行 HTTP 请求（支持自动二次请求）
   */
  const performHttpSend = async (text, thinkingId, attachmentsToSent = []) => {
    try {
      const response = await transport.httpSend({
        message: text,
        sessionId: sessionManager.currentSessionId.value,
        attachments: attachmentsToSent
      })

      // 处理后端生成的 UUID (新建会话模式)
      if (response.choices?.[0]?.finish_reason === 'new_session') {
        const newId = response.choices[0].message.content
        console.log('HTTP 模式同步新会话 ID 并触发标题生成:', newId)
        
        // 1. 设置当前会话 ID (静默选择)
        sessionManager.selectSession({ session_id: newId, title: '新会话', enable_markdown: enableMarkdownDefault.value }, null, false, false)
        
        // 同步新建会话的 Markdown 设置
        if (enableMarkdownDefault.value) {
          chatApi.updateSessionSetting(newId, true).catch(() => {})
        }
        
        // 2. 收到 ID 后立即调用标题生成
        sessionManager.updateSessionTitle(newId, text)
        
        // 3. 自动发起第二次真实请求
        return performHttpSend(text, thinkingId, attachmentsToSent)
      }

      messageProcessor.processAiResponse(chatState.messages, response, thinkingId, chatState.scrollToBottom)

      nextTick(() => chatState.scrollToBottom())
    } catch (err) {
      messageProcessor.removeThinkingMessage(chatState.messages, thinkingId)
      ElMessage.error(err.message || '发送失败')
    } finally {
      // 检查当前是否还有排队的请求（通过判断是否还有 thinking）
      const hasThinking = chatState.messages.value.some(m => m.role === 'thinking')
      if (!hasThinking) {
        chatState.loading.value = false
      }
    }
  }

  /**
   * WebSocket 方式发送消息
   */
  const wsSend = async () => {
    if (!chatState.inputMsg.value.trim() && attachments.value.length === 0) return
    
    const text = chatState.inputMsg.value
    const userMsgId = Date.now()
    
    // 清理之前的 thinking 占位，保持只有一个 thinking 标签
    messageProcessor.cleanupThinkingMessage(chatState.messages)
    
    // 使用更可靠的 ID 防止极速点击下的冲突
    const thinkingId = `thinking_${userMsgId}_${Math.random().toString(36).substr(2, 4)}`
    // request_id 使用唯一的标识符
    const requestId = `req_${userMsgId}_${Math.random().toString(36).substr(2, 4)}`

    // 添加用户消息并绑定 request_id
    const attachmentsToSent = attachments.value.map(a => a.path)
    chatState.addMessage({ 
      id: userMsgId, 
      role: 'user', 
      content: text, 
      attachments: attachmentsToSent,
      created_at: Date.now() / 1000,
      request_id: requestId
    })
    
    chatState.inputMsg.value = ''
    attachments.value = []
    chatState.loading.value = true
    nextTick(() => chatState.scrollToBottom())

    // 添加新的 thinking 消息
    chatState.addMessage({ 
      id: thinkingId, 
      role: 'thinking', 
      content: 'Thinking...' 
    })
    
    // 直接包装需要传递给 transport.wsSend 的 callbacks 选项
    const callbacks = {
      thinkingId,
      onContent: (text, turn, thinkingIdParam, finishReason, responseId, requestIdParam) => {
        messageProcessor.processStreamContent(chatState.messages, text, turn, thinkingId, finishReason, responseId, requestIdParam)
      },
      onToolStart: (toolCall, thinkingIdParam, responseId, requestIdParam) => {
        messageProcessor.processStreamToolStart(chatState.messages, toolCall, thinkingId, responseId, requestIdParam)
      },
      onToolEnd: (toolEnd, responseId, requestIdParam) => {
        messageProcessor.processStreamToolEnd(chatState.messages, toolEnd, responseId, requestIdParam)
      },
      onError: (errorMessage, thinkingIdParam, requestIdParam) => {
        messageProcessor.processStreamError(chatState.messages, errorMessage, thinkingId)
        ElMessage.error(errorMessage || '流式对话过程出错')
      },
      onSessionId: (newSessionId) => {
        console.log('WS 模式同步新会话 ID 并触发标题生成:', newSessionId)
        // 1. 更新本地状态（静默同步）
        sessionManager.selectSession({ session_id: newSessionId, title: '新会话', enable_markdown: enableMarkdownDefault.value }, null, false, false)
        
        // 同步新建会话的 Markdown 设置
        if (enableMarkdownDefault.value) {
          chatApi.updateSessionSetting(newSessionId, true).catch(() => {})
        }
        
        // 2. 收到 ID 后立即调用标题生成
        sessionManager.updateSessionTitle(newSessionId, text)
      },
      onComplete: (data, thinkingIdParam, requestIdParam) => {
        // 精确清理对应的 thinking 占位符
        messageProcessor.removeThinkingMessage(chatState.messages, thinkingId)
        
        // 尝试从 history 中提取最后一条消息覆盖当前流式消息，以显示后端清洗后的格式（如剥离 Markdown）
        if (data && data.history && data.history.length > 0) {
          const lastHistoryMsg = data.history[data.history.length - 1]
          if (lastHistoryMsg && lastHistoryMsg.role === 'assistant' && lastHistoryMsg.content) {
             // 强制更新引用，确保视图响应
             const newMessages = [...chatState.messages.value]
             const targetIdx = newMessages.findIndex(m => m.role === 'assistant' && !m.tool_calls && m.request_id === requestIdParam)
             
             if (targetIdx !== -1) {
               newMessages[targetIdx] = { ...newMessages[targetIdx], content: lastHistoryMsg.content }
               chatState.messages.value = newMessages
             } else {
                // 如果没有带有 request_id 的普通内容消息，则找最后一个 assistant
                const lastIdx = newMessages.map(m => m.role).lastIndexOf('assistant')
                if (lastIdx !== -1 && !newMessages[lastIdx].tool_calls) {
                  newMessages[lastIdx] = { ...newMessages[lastIdx], content: lastHistoryMsg.content }
                  chatState.messages.value = newMessages
                }
             }
          }
        }
      },
      scrollToBottom: () => nextTick(() => chatState.scrollToBottom()),
      setLoading: (val) => { chatState.loading.value = val }
    }
    
    try {
      await transport.wsSend({
        message: text,
        sessionId: sessionManager.currentSessionId.value,
        attachments: attachmentsToSent,
        requestId,
        callbacks
      })
    } catch (e) {
      console.error('WebSocket发送失败:', e)
      ElMessage.error('WebSocket 消息发送失败，将使用 HTTP 模式')
      messageProcessor.removeThinkingMessage(chatState.messages, transport.getCurrentThinkingId())
      chatState.loading.value = false
      transport.setTransportMode('http')
    }
  }

  // ==================== 会话选择 ====================
  
  /**
   * 选择会话
   * @param {Object} session - 会话对象
   */
  const selectSession = (session) => {
    sessionManager.selectSession(session, transport.disconnectWebSocket)
    chatState.clearMessages()
    chatState.inputMsg.value = ''
    // 切换会话时重置加载状态，解除模式锁定
    chatState.loading.value = false
  }

  /**
   * 新建会话
   */
  const createNewSession = () => {    
    transport.setTransportMode('ws', transport.disconnectWebSocket)
    sessionManager.createNewSession(transport.disconnectWebSocket)
    chatState.clearMessages()
    chatState.inputMsg.value = ''
    // 新建会话时重置加载状态，解除模式锁定
    chatState.loading.value = false
  }

  // ==================== 滚动事件 ====================
  
  /**
   * 处理滚动事件
   */
  const handleScroll = () => {
    if (!chatState.messageList.value || !sessionManager.hasMore.value || sessionManager.historyLoading.value) return
    
    if (sessionManager.checkHasMore()) return
    
    if (chatState.messageList.value.scrollTop < 50) {
      sessionManager.loadSessionHistory(2).then(historyData => {
        if (historyData && historyData.length > 0) {
          chatState.insertMessage(0, historyData, true)
        }
      })
    }
  }

  /**
   * 绑定滚动事件
   */
  const bindScrollEvent = () => {
    if (chatState.messageList.value) {
      chatState.messageList.value.addEventListener('scroll', handleScroll)
    }
  }

  /**
   * 移除滚动事件
   */
  const unbindScrollEvent = () => {
    if (chatState.messageList.value) {
      chatState.messageList.value.removeEventListener('scroll', handleScroll)
    }
  }

  // ==================== 返回导出 ====================
  return {
    // 状态 - 消息相关
    messages: chatState.messages,
    inputMsg: chatState.inputMsg,
    loading: chatState.loading,
    messageList: chatState.messageList,
    
    // 新增附件状态导出
    attachments,
    enableMarkdownDefault,

    // 状态 - 会话相关
    sessions: sessionManager.sessions,
    sessionsLoading: sessionManager.sessionsLoading,
    currentSessionId: sessionManager.currentSessionId,
    typingSessionId: sessionManager.typingSessionId,
    activeCollapse: sessionManager.activeCollapse,
    hasMore: sessionManager.hasMore,
    sessionCreating: sessionManager.sessionCreating,
    
    // 状态 - 通信相关
    transportMode: transport.transportMode,
    wsConnected: transport.wsConnected,
    
    // 方法 - 会话
    loadSessions: sessionManager.loadSessions,
    handleDeleteSession: sessionManager.handleDeleteSession,
    selectSession,
    createNewSession,
    
    // 方法 - 发送
    send,
    httpSend,
    wsSend,
    initWebSocket: transport.initWebSocket,
    disconnectWebSocket: transport.disconnectWebSocket,
    setTransportMode: (mode) => transport.setTransportMode(mode, transport.disconnectWebSocket),
    
    // 工具函数
    formatTimestamp,
    isToolCall,
    isToolResult,
    getToolName,
    getToolArguments,
    getToolResultName,
    getToolResultContent,
    getMessageTimestamp,
    
    // 滚动事件
    handleScroll,
    bindScrollEvent,
    unbindScrollEvent
  }
}
