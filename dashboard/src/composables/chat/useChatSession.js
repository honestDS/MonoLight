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
    if (!chatState.inputMsg.value.trim() || chatState.loading.value) return
    
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
          (session, dc, disconnect) => sessionManager.selectSession(session, dc, disconnect),
          thinkingId
        )
      }

      if (!isNewSession) {
        messageProcessor.processAiResponse(chatState.messages, response, thinkingId, chatState.scrollToBottom)
        nextTick(() => chatState.scrollToBottom())
      }
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
    if (!chatState.inputMsg.value.trim() || chatState.loading.value) return
    
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
    
    // 设置 WebSocket 消息处理回调
    const wsOptions = {
      onMessage: (data) => {
        transport.handleWsMessage(data, {
          onToolCall: (toolCall) => {
            messageProcessor.handleToolCallMessage(chatState.messages, toolCall, chatState.scrollToBottom)
          },
          onComplete: (data, thinkingId) => {
            // 处理新会话
            if (!sessionManager.currentSessionId.value) {
              messageProcessor.handleNewSession(
                sessionManager.sessions,
                (session, dc, disconnect) => sessionManager.selectSession(session, dc, disconnect),
                thinkingId,
                false
              )
            } else {
              messageProcessor.processAiResponse(chatState.messages, data, thinkingId, chatState.scrollToBottom)
            }
          },
          scrollToBottom: () => nextTick(() => chatState.scrollToBottom()),
          setLoading: (val) => { chatState.loading.value = val }
        })
      }
    }
    
    try {
      await transport.wsSend({
        message: text,
        sessionId: sessionManager.currentSessionId.value,
        wsOptions
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
    transport.setTransportMode('http', transport.disconnectWebSocket)
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
    activeCollapse: sessionManager.activeCollapse,
    hasMore: sessionManager.hasMore,
    
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