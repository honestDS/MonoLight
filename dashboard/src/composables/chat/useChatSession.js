/**
 * 聊天会话管理 composable
 * 组合消息状态、会话管理、通信层、消息处理等模块
 * 保持与原有 API 兼容
 */
import { nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { useChatState } from './useChatState'
import { useSessionManager } from './useSessionManager'
import { useChatTransport } from './useChatTransport'
import { useMessageProcessor } from './useMessageProcessor'
import { formatTimestamp, isToolCall, isToolResult, getToolName, getToolArguments, getToolResultName, getToolResultContent, getMessageTimestamp } from '../../utils'

export function useChatSession() {
  // ==================== 组合各模块 ====================
  
  // 1. 消息状态
  const chatState = useChatState()
  
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
    if (!chatState.inputMsg.value.trim()) return
    
    const text = chatState.inputMsg.value
    const userMsgId = Date.now()
    
    // 添加用户消息
    chatState.addMessage({
      id: userMsgId,
      role: 'user',
      content: text,
      created_at: Date.now() / 1000
    })
    
    chatState.inputMsg.value = ''
    chatState.loading.value = true
    nextTick(() => chatState.scrollToBottom())

    const thinkingId = Date.now() + 1
    chatState.addMessage({ id: thinkingId, role: 'thinking', content: 'Thinking...' })

    try {
      const response = await transport.httpSend({
        message: text,
        sessionId: sessionManager.currentSessionId.value
      })

      let isNewSession = false
      if (!sessionManager.currentSessionId.value) {
        isNewSession = true
        await messageProcessor.handleNewSession(
          sessionManager.sessions,
          (session, dc, disconnect) => sessionManager.selectSession(session, dc, disconnect, false),
          thinkingId,
          sessionManager.sessionCreating
        )
      }

      messageProcessor.processAiResponse(chatState.messages, response, thinkingId, chatState.scrollToBottom)

      // 如果是新会话，异步请求生成标题
      if (isNewSession && sessionManager.currentSessionId.value) {
        sessionManager.updateSessionTitle(sessionManager.currentSessionId.value, text)
      }
      nextTick(() => chatState.scrollToBottom())
    } catch (err) {
      messageProcessor.removeThinkingMessage(chatState.messages, thinkingId)
      ElMessage.error(err.message || '发送失败')
    } finally {
      chatState.loading.value = false
    }
  }

  /**
   * WebSocket 方式发送消息
   */
  const wsSend = async () => {
    if (!chatState.inputMsg.value.trim()) return
    
    const text = chatState.inputMsg.value
    const userMsgId = Date.now()
    transport.setCurrentThinkingId(userMsgId + 1)
    
    // 添加用户消息
    chatState.addMessage({ 
      id: userMsgId, 
      role: 'user', 
      content: text, 
      created_at: Date.now() / 1000 
    })
    
    chatState.inputMsg.value = ''
    chatState.loading.value = true
    nextTick(() => chatState.scrollToBottom())
    
    // 添加 thinking 消息
    chatState.addMessage({ 
      id: transport.getCurrentThinkingId(), 
      role: 'thinking', 
      content: 'Thinking...' 
    })
    
    // 直接包装需要传递给 transport.wsSend 的 callbacks 选项
    const callbacks = {
      onContent: (text, turn, thinkingId) => {
        messageProcessor.processStreamContent(chatState.messages, text, turn, thinkingId)
      },
      onToolStart: (toolCall) => {
        messageProcessor.processStreamToolStart(chatState.messages, toolCall)
      },
      onToolEnd: (toolEnd) => {
        messageProcessor.processStreamToolEnd(chatState.messages, toolEnd)
      },
      onError: (errorMessage, thinkingId) => {
        messageProcessor.processStreamError(chatState.messages, errorMessage, thinkingId)
        ElMessage.error(errorMessage || '流式对话过程出错')
      },
      onComplete: (data, thinkingId) => {
        const isNewSession = !sessionManager.currentSessionId.value
        // 处理新会话
        if (isNewSession) {
          messageProcessor.handleNewSession(
            sessionManager.sessions,
            (session, dc, disconnect) => sessionManager.selectSession(session, dc, disconnect, false),
            thinkingId,
            false,
            sessionManager.sessionCreating
          )
        }

        // 如果是新会话，异步请求生成标题
        if (isNewSession && data.session_id) {
          sessionManager.updateSessionTitle(data.session_id, text)
        }

        // 清理 thinking 占位符
        messageProcessor.cleanupThinkingMessage(chatState.messages)
      },
      scrollToBottom: () => nextTick(() => chatState.scrollToBottom()),
      setLoading: (val) => { chatState.loading.value = val }
    }
    
    try {
      await transport.wsSend({
        message: text,
        sessionId: sessionManager.currentSessionId.value,
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
  }

  /**
   * 新建会话
   */
  const createNewSession = () => {    
    transport.setTransportMode('ws', transport.disconnectWebSocket)
    sessionManager.createNewSession(transport.disconnectWebSocket)
    chatState.clearMessages()
    chatState.inputMsg.value = ''
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