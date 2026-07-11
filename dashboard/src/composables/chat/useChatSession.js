// 聊天会话管理 composable，聚合状态、会话、通信与消息处理
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useChatState } from './useChatState'
import { useSessionManager } from './useSessionManager'
import { useChatTransport } from './useChatTransport'
import { useMessageProcessor } from './useMessageProcessor'
import { formatTimestamp, isToolCall, isToolResult, getToolName, getToolArguments, getToolResultName, getToolResultContent, getMessageTimestamp, normalizeMessageContent, getMessageDedupeKeys } from '../../utils'
import { chatApi } from '../../api'
import i18n from '../../i18n'

const t = (key, ...args) => i18n.global.t(key, ...args)
const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms))

const normalizeHistoryMessage = (message) => {
  const content = normalizeMessageContent(message?.content)
  if (message?.type === 'background_result' && content?.type === 'background_tool_result') {
    return {
      ...message,
      db_id: message.db_id || message.id,
      role: 'background_system',
      content: JSON.stringify(content)
    }
  }
  return message
}

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

  const currentSession = computed(() =>
    sessionManager.sessions.value.find(
      session => session.session_id === sessionManager.currentSessionId.value
    ) || null
  )
  const isCurrentSessionReadOnly = computed(() => {
    const source = currentSession.value?.source
    return Boolean(source && !['http', 'ws'].includes(source))
  })

  const rejectReadOnlySession = () => {
    if (!isCurrentSessionReadOnly.value) return false
    ElMessage.warning(t('chat.external_session_read_only'))
    return true
  }

  // ==================== 设置模块间连接 ====================
  
  // 设置会话管理的历史记录加载回调
  sessionManager.setLoadHistoryCallback(async (pageCount) => {
    const historyData = await sessionManager.loadSessionHistory(pageCount)
    if (historyData && historyData.length > 0) {
      // 插入到消息列表开头
      chatState.insertMessage(0, historyData.map(normalizeHistoryMessage), true)
      await nextTick()
      chatState.scrollToBottom()
    }
  })

  const mergeLatestSessionHistory = async (sessionId = sessionManager.currentSessionId.value) => {
    if (!sessionId || sessionId !== sessionManager.currentSessionId.value) return
    const res = await chatApi.sessionsHistory(sessionId, 1, 20)
    if (sessionId !== sessionManager.currentSessionId.value) return
    const historyData = res.data?.data || []
    if (!historyData.length) return

    const existingKeys = new Set(chatState.messages.value.flatMap(m => [...getMessageDedupeKeys(m)]))
    const newMessages = []
    for (const item of historyData) {
      const message = normalizeHistoryMessage({ ...item, db_id: item.id })
      const messageKeys = getMessageDedupeKeys(message)
      if ([...messageKeys].some(key => existingKeys.has(key))) continue
      newMessages.push(message)
      messageKeys.forEach(key => existingKeys.add(key))
    }
    if (newMessages.length) {
      chatState.messages.value.push(...newMessages)
      await nextTick()
      chatState.scrollToBottom()
    }
  }

  const pollBackgroundTasksUntilSettled = async (sessionId, intervalSeconds = 2) => {
    if (!sessionId) return
    const maxAttempts = 60
    for (let attempt = 0; attempt < maxAttempts; attempt++) {
      await sleep(Math.max(1, intervalSeconds) * 1000)
      if (sessionId !== sessionManager.currentSessionId.value) return
      await mergeLatestSessionHistory(sessionId)

      const res = await chatApi.backgroundTasks({ session_id: sessionId, page: 1, size: 20 })
      if (sessionId !== sessionManager.currentSessionId.value) return
      const tasks = res.data?.data || []
      const hasUnfinishedTasks = tasks.some(task => ['pending', 'running'].includes(task.status) || ['pending', 'running'].includes(task.reply_status))
      if (!hasUnfinishedTasks) {
        await mergeLatestSessionHistory(sessionId)
        return
      }
    }
  }

  // ==================== 核心发送方法 ====================

  /**
   * 仅用于连续发送时插入一条携带 queued 状态的消息占位，并直接发送
   */
  const enqueueMessage = (text, attachments = []) => {
    if (rejectReadOnlySession()) return

    const userMsgId = Date.now() + Math.random()
    
    // 添加用户消息到界面，增加排队标记
    const attachmentsToSent = attachments.map(a => a.path)
    chatState.addMessage({
      id: userMsgId,
      role: 'user',
      content: text,
      attachments: attachmentsToSent,
      created_at: Date.now() / 1000,
      status: 'queued' // 自定义状态，仅用于样式展示
    })
    
    nextTick(() => chatState.scrollToBottom())
    
    // 不排队，直接调用底层的发送机制
    if (transport.transportMode.value === 'ws') {
      wsSend(text, attachmentsToSent, userMsgId)
    } else {
      httpSend(text, attachmentsToSent, userMsgId)
    }
  }

  /**
   * 发送消息（统一入口）
   */
  const send = async () => {
    if (rejectReadOnlySession()) return
    if (transport.transportMode.value === 'ws') {
      return wsSend(chatState.inputMsg.value, attachments.value.map(a => a.path))
    } else {
      return httpSend(chatState.inputMsg.value, attachments.value.map(a => a.path))
    }
  }

  /**
   * HTTP 方式发送消息
   */
  const httpSend = async (textParam = null, attachmentsParam = null, existingMsgId = null) => {
    if (rejectReadOnlySession()) return

    const text = textParam !== null ? textParam : chatState.inputMsg.value
    const attachmentsToSent = attachmentsParam !== null ? attachmentsParam : attachments.value.map(a => a.path)
    
    if (!text.trim() && attachmentsToSent.length === 0) return
    
    const userMsgId = existingMsgId || Date.now()
    
    // 统一渲染顺序：清理之前的 thinking 占位，确保 AI 响应紧跟最新消息（与流式行为一致）
    messageProcessor.cleanupThinkingMessage(chatState.messages)
    
    // 如果没有现成的消息 ID（非队列来的），则添加用户消息
    if (!existingMsgId) {
      chatState.addMessage({
        id: userMsgId,
        role: 'user',
        content: text,
        attachments: attachmentsToSent,
        created_at: Date.now() / 1000
      })
      
      chatState.inputMsg.value = ''
      attachments.value = []
    } else {
      // 并不直接删除队列标记，而是等响应回来后再清理（为了保留视觉效果直到收到响应）
      // 由于这是 HTTP 发送逻辑的入口，我们在此将 loading 状态标记为 true。
    }
    chatState.loading.value = true
    nextTick(() => chatState.scrollToBottom())

    const thinkingId = userMsgId + 1
    chatState.addMessage({ id: thinkingId, role: 'thinking', content: 'Thinking...' })

    await performHttpSend(text, thinkingId, attachmentsToSent, userMsgId, sessionManager.currentSessionId.value)
  }

  /**
   * 实际执行 HTTP 请求（支持自动二次请求）
   */
  const performHttpSend = async (text, thinkingId, attachmentsToSent = [], userMsgId = null, requestSessionId = null) => {
    try {
      const response = await transport.httpSend({
        message: text,
        sessionId: requestSessionId,
        attachments: attachmentsToSent
      })

      // 处理后端生成的 UUID (新建会话模式)
      if (response.choices?.[0]?.finish_reason === 'new_session') {
        if (requestSessionId !== sessionManager.currentSessionId.value) return
        const newId = response.choices[0].message.content
        console.log('HTTP 模式同步新会话 ID 并触发标题生成:', newId)
        
        // 1. 设置当前会话 ID (静默选择)
        sessionManager.selectSession({ session_id: newId, title: t('chat.default_title'), enable_markdown: enableMarkdownDefault.value }, null, false, false)
        
        // 同步新建会话的 Markdown 设置
        if (enableMarkdownDefault.value) {
          chatApi.updateSessionSetting(newId, true).catch(() => {})
        }
        
        // 2. 收到 ID 后立即调用标题生成
        sessionManager.updateSessionTitle(newId, text)
        
        // 3. 自动发起第二次真实请求
        return performHttpSend(text, thinkingId, attachmentsToSent, userMsgId, newId)
      }

      if (requestSessionId !== sessionManager.currentSessionId.value) return

      // 成功收到响应时清除当前消息的排队标记
      const userMsg = chatState.messages.value.find(m => m.id === userMsgId && m.role === 'user')
      if (userMsg && userMsg.status === 'queued') {
        delete userMsg.status
      }

      messageProcessor.processAiResponse(chatState.messages, response, thinkingId, chatState.scrollToBottom)

      if (response.has_background_tasks) {
        void pollBackgroundTasksUntilSettled(requestSessionId, response.background_task_poll_interval || 2).catch(err => {
          console.error('Background task polling failed:', err)
        })
      }

      nextTick(() => chatState.scrollToBottom())
    } catch (err) {
      if (requestSessionId !== sessionManager.currentSessionId.value) return
      // 出错时也应清除排队标记，以便重新发送或其他操作
      const userMsg = chatState.messages.value.find(m => m.id === userMsgId && m.role === 'user')
      if (userMsg && userMsg.status === 'queued') {
        delete userMsg.status
      }
      messageProcessor.removeThinkingMessage(chatState.messages, thinkingId)
      ElMessage.error(err.message || t('chat.send_failed'))
    } finally {
      if (requestSessionId === sessionManager.currentSessionId.value) {
        // 检查当前是否还有排队的请求（通过判断是否还有 thinking）
        const hasThinking = chatState.messages.value.some(m => m.role === 'thinking')
        if (!hasThinking) {
          chatState.loading.value = false
        }
      }
    }
  }

  /**
   * WebSocket 方式发送消息
   */
  const wsSend = async (textParam = null, attachmentsParam = null, existingMsgId = null) => {
    if (rejectReadOnlySession()) return

    const text = textParam !== null ? textParam : chatState.inputMsg.value
    const attachmentsToSent = attachmentsParam !== null ? attachmentsParam : attachments.value.map(a => a.path)
    
    if (!text.trim() && attachmentsToSent.length === 0) return
    
    const userMsgId = existingMsgId || Date.now()
    
    // 清理之前的 thinking 占位，保持只有一个 thinking 标签
    messageProcessor.cleanupThinkingMessage(chatState.messages)
    
    // 使用更可靠的 ID 防止极速点击下的冲突
    const thinkingId = `thinking_${userMsgId}_${Math.random().toString(36).substr(2, 4)}`
    // request_id 使用唯一的标识符
    const requestId = `req_${userMsgId}_${Math.random().toString(36).substr(2, 4)}`

    // 如果没有现成的消息 ID（非队列来的），则添加用户消息
    if (!existingMsgId) {
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
    } else {
      // 并不直接删除队列标记，而是等响应回来后再清理（为了保留视觉效果直到收到响应）
      // 仅更新 request_id
      const msg = chatState.messages.value.find(m => m.id === existingMsgId)
      if (msg) {
        msg.request_id = requestId
      }
    }
    chatState.loading.value = true
    nextTick(() => chatState.scrollToBottom())

    // 添加新的 thinking 消息
    chatState.addMessage({ 
      id: thinkingId, 
      role: 'thinking', 
      content: 'Thinking...' 
    })
    
    let requestSessionId = sessionManager.currentSessionId.value
    const isCurrentRequestSession = () => requestSessionId === sessionManager.currentSessionId.value

    // 直接包装需要传递给 transport.wsSend 的 callbacks 选项
    const callbacks = {
      thinkingId,
      onTaskStart: () => {
        if (!isCurrentRequestSession()) return
        // 调度器明确发来了合并/开始信号，说明它正在处理最新的队列消息了，清除队列视觉状态
        chatState.messages.value.forEach(m => {
          if (m.role === 'user' && m.status === 'queued') {
            delete m.status
          }
        })
      },
      onContent: (text, turn, thinkingIdParam, finishReason, responseId, requestIdParam) => {
        if (!isCurrentRequestSession()) return
        messageProcessor.processStreamContent(chatState.messages, text, turn, thinkingId, finishReason, responseId, requestIdParam)
      },
      onToolStart: (toolCall, thinkingIdParam, responseId, requestIdParam) => {
        if (!isCurrentRequestSession()) return
        messageProcessor.processStreamToolStart(chatState.messages, toolCall, thinkingId, responseId, requestIdParam)
      },
      onToolEnd: (toolEnd, responseId, requestIdParam) => {
        if (!isCurrentRequestSession()) return
        messageProcessor.processStreamToolEnd(chatState.messages, toolEnd, responseId, requestIdParam)
      },
      onError: (errorMessage, thinkingIdParam, requestIdParam) => {
        if (!isCurrentRequestSession()) return
        messageProcessor.processStreamError(chatState.messages, errorMessage, thinkingId)
        // 同一会话仅有一个调度任务，任务异常结束意味着所有追加消息一并失败：
        // 清除全部排队消息的视觉状态，并清理残留的 thinking 占位（含追加消息产生的占位）
        chatState.messages.value.forEach(m => {
          if (m.role === 'user' && m.status === 'queued') {
            delete m.status
          }
        })
        messageProcessor.cleanupThinkingMessage(chatState.messages)
        ElMessage.error(errorMessage || t('chat.stream_error'))
      },
      onProactiveReply: (data) => {
        if (data.session_id && data.session_id !== sessionManager.currentSessionId.value) return
        void mergeLatestSessionHistory(data.session_id || sessionManager.currentSessionId.value).catch(err => {
          console.error('Proactive reply history merge failed:', err)
        })
      },
      onProactiveReplyError: (data) => {
        if (data.session_id && data.session_id !== sessionManager.currentSessionId.value) return
        const errorMessage = data.content || data.message || 'Background proactive reply failed'
        messageProcessor.processStreamError(chatState.messages, errorMessage, null)
        ElMessage.error(errorMessage)
        void mergeLatestSessionHistory(data.session_id || sessionManager.currentSessionId.value).catch(err => {
          console.error('Proactive reply error history merge failed:', err)
        })
      },
      onSessionId: (newSessionId) => {
        if (requestSessionId !== sessionManager.currentSessionId.value) return
        requestSessionId = newSessionId
        console.log('WS 模式同步新会话 ID 并触发标题生成:', newSessionId)
        // 1. 更新本地状态（静默同步）
        sessionManager.selectSession({ session_id: newSessionId, title: t('chat.default_title'), enable_markdown: enableMarkdownDefault.value }, null, false, false)
        
        // 同步新建会话的 Markdown 设置
        if (enableMarkdownDefault.value) {
          chatApi.updateSessionSetting(newSessionId, true).catch(() => {})
        }
        
        // 2. 收到 ID 后立即调用标题生成
        sessionManager.updateSessionTitle(newSessionId, text)
      },
      onComplete: (data, thinkingIdParam, requestIdParam, eventType) => {
        if (!isCurrentRequestSession()) return
        if (data.session_id && data.session_id !== requestSessionId) return
        chatState.messages.value.forEach(m => {
          if (m.role === 'user' && m.status === 'queued') {
            delete m.status
          }
        })
        
        // 如果是基于 response_id 变更（新轮次结束）触发的精准替换
        if (eventType === 'turn_end' && data.response_id && data.content) {
          const newMessages = [...chatState.messages.value]
          const targetIdx = newMessages.findIndex(m => m.response_id === data.response_id && m.role === 'assistant')
          if (targetIdx !== -1) {
            newMessages[targetIdx] = { ...newMessages[targetIdx], content: data.content }
            chatState.messages.value = newMessages
          }
          return // turn_end 时不需要执行 done 的历史比对和占位符清理
        }

        if (data.files && data.files.length > 0) {
          const newMessages = [...chatState.messages.value]
          let targetIdx = -1

          if (data.response_id) {
            targetIdx = newMessages.findLastIndex(m => m.response_id === data.response_id && m.role === 'assistant' && !isToolCall(m))
          }

          if (targetIdx === -1 && requestIdParam) {
            targetIdx = newMessages.findLastIndex(m => m.request_id === requestIdParam && m.role === 'assistant' && !isToolCall(m))
          }

          if (targetIdx === -1 && !data.response_id && !requestIdParam) {
            targetIdx = newMessages.findLastIndex(m => m.role === 'assistant' && !isToolCall(m))
          }

          if (targetIdx !== -1) {
            newMessages[targetIdx] = { ...newMessages[targetIdx], files: data.files }
            chatState.messages.value = newMessages
          } else {
            messageProcessor.processAiResponse(chatState.messages, data, thinkingId, chatState.scrollToBottom)
            return
          }
        }

        // 流式正文和工具消息已经按事件逐条渲染，结束事件只负责清理占位符。
        messageProcessor.removeThinkingMessage(chatState.messages, thinkingId)
      },
      scrollToBottom: () => {
        if (isCurrentRequestSession()) nextTick(() => chatState.scrollToBottom())
      },
      setLoading: (val) => {
        if (isCurrentRequestSession()) chatState.loading.value = val
      }
    }
    
    try {
      await transport.wsSend({
        message: text,
        sessionId: requestSessionId,
        attachments: attachmentsToSent,
        requestId,
        callbacks
      })
    } catch (e) {
      console.error('WebSocket发送失败:', e)
      if (!isCurrentRequestSession()) return
      ElMessage.error(t('chat.ws_send_failed'))
      messageProcessor.removeThinkingMessage(chatState.messages, thinkingId)
      chatState.loading.value = false
      transport.setTransportMode('http')
    }
  }

  // ==================== 会话选择 ====================
  
  // 选择会话；session 为会话对象
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
          chatState.insertMessage(0, historyData.map(normalizeHistoryMessage), true)
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
    currentSession,
    isCurrentSessionReadOnly,
    
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
    enqueueMessage,
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
