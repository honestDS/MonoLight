// 聊天会话管理 composable，聚合状态、会话、通信与消息处理
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useChatState } from './useChatState'
import { useSessionManager } from './useSessionManager'
import { useChatTransport } from './useChatTransport'
import { useMessageProcessor } from './useMessageProcessor'
import { clearAllContextSummaryWorks, clearContextSummaryRequest, endContextSummaryWork, shouldIgnoreExternalSessionEvent, startContextSummaryWork } from './contextSummaryTracker.mjs'
import { finishWorkLifecycle, markInputQueued, markInputsDequeued, resetWorkLifecycle, startAgentLoop, stopAgentLoop } from './workLifecycleTracker.mjs'
import { formatTimestamp, isToolCall, isToolResult, getToolCalls, getToolCallName, getToolCallArguments, getToolCallContent, getToolResultName, getToolResultContent, getMessageTimestamp, normalizeMessageContent, getMessageDedupeKeys, findMessageReplacementIndex, mergeRemoteMessage, mergeRemoteMessageIntoList } from '../../utils'
import { chatApi } from '../../api'
import i18n from '../../i18n'

const t = (key, ...args) => i18n.global.t(key, ...args)
const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms))

const normalizeHistoryMessage = (message) => {
  const normalizedMessage = {
    ...message,
    db_id: message?.db_id ?? message?.id
  }
  const content = normalizeMessageContent(message?.content)
  if (message?.type === 'background_result' && content?.type === 'background_tool_result') {
    return {
      ...normalizedMessage,
      role: 'background_system',
      content: JSON.stringify(content)
    }
  }
  return normalizedMessage
}

const getAuditConfirmationRecordId = (message) => {
  if (message?.type !== 'audit_confirmation') return null
  try {
    const payload = typeof message.content === 'string' ? JSON.parse(message.content) : message.content
    return payload?.audit_record_id ? String(payload.audit_record_id) : null
  } catch {
    return null
  }
}

const parseAuditConfirmationResponse = (response) => {
  const content = response?.choices?.[0]?.message?.content
  try {
    const payload = typeof content === 'string' ? JSON.parse(content) : content
    return payload?.type === 'audit_confirmation' ? payload : null
  } catch {
    return null
  }
}

const getLocalMessageType = (message) => {
  if (message?.type === 'audit_decision' && message.role === 'user') return 'user'
  if (message?.type && message.type !== 'text') return message.type
  if (isToolCall(message)) return 'tool_call'
  if (isToolResult(message)) return 'tool_result'
  return message?.role || message?.type || 'message'
}

const findTransientHistoryMessageIndex = (messages, historyMessage) => {
  const historyContent = normalizeMessageContent(historyMessage?.content)
  const historyType = getLocalMessageType(historyMessage)
  return messages.findIndex(message => {
    if (message?.db_id) return false
    if (getLocalMessageType(message) !== historyType) return false
    return JSON.stringify(normalizeMessageContent(message?.content)) === JSON.stringify(historyContent)
  })
}

export function useChatSession() {
  // ==================== 组合各模块 ====================

  // 1. 消息状态
  const chatState = useChatState()
  
  // 新增附件状态
  const attachments = ref([])
  const contextSummaryWorkKeys = ref(new Set())
  const contextSummaryRequestKeys = new Map()
  const sessionEventSequenceBySession = new Map()
  const initialHistoryLoaded = ref(true)
  
  // 默认 Markdown 开关状态（用于未选择会话时）
  const enableMarkdownDefault = ref(false)
  
  // 2. 会话管理
  const sessionManager = useSessionManager()
  const isContextSummarizing = computed(() => contextSummaryWorkKeys.value.size > 0)
  
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

  const applyLifecycleEvent = (updateMessages, event, isCurrentRequestSession) => {
    if (!isCurrentRequestSession()) return
    const currentSessionId = sessionManager.currentSessionId.value
    if (event?.session_id && currentSessionId && event.session_id !== currentSessionId) return

    chatState.messages.value = updateMessages(chatState.messages.value, event)
    void nextTick(() => {
      if (isCurrentRequestSession()) chatState.scrollToBottom()
    })
  }

  const createLifecycleCallbacks = isCurrentRequestSession => ({
    onInputQueued: event => applyLifecycleEvent(markInputQueued, event, isCurrentRequestSession),
    onInputDequeued: event => applyLifecycleEvent(markInputsDequeued, event, isCurrentRequestSession),
    onAgentLoopStart: event => applyLifecycleEvent(startAgentLoop, event, isCurrentRequestSession),
    onAgentLoopOutput: event => applyLifecycleEvent(stopAgentLoop, event, isCurrentRequestSession),
    onWorkFinished: event => applyLifecycleEvent(finishWorkLifecycle, event, isCurrentRequestSession)
  })

  const finishRequestLifecycle = (requestId, isCurrentRequestSession) => {
    if (!requestId) return
    applyLifecycleEvent(
      finishWorkLifecycle,
      { request_ids: [requestId] },
      isCurrentRequestSession
    )
  }

  const rejectReadOnlySession = () => {
    if (!isCurrentSessionReadOnly.value) return false
    ElMessage.warning(t('chat.external_session_read_only'))
    return true
  }

  let restoringHistoryScroll = false

  // ==================== 设置模块间连接 ====================
  
  // 设置会话管理的历史记录加载回调
  sessionManager.setLoadHistoryCallback(async (pageCount) => {
    const requestedSessionId = sessionManager.currentSessionId.value
    const historyData = await sessionManager.loadSessionHistory(pageCount)
    if (requestedSessionId !== sessionManager.currentSessionId.value) return

    restoringHistoryScroll = true
    try {
      if (historyData && historyData.length > 0) {
        // 插入到消息列表开头
        chatState.insertMessage(0, historyData.map(normalizeHistoryMessage), true)
      }
      initialHistoryLoaded.value = true
      await nextTick()
      if (historyData && historyData.length > 0) {
        await chatState.scrollToBottom('smooth')
      }
    } finally {
      requestAnimationFrame(() => {
        restoringHistoryScroll = false
      })
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
      const auditRecordId = getAuditConfirmationRecordId(message)
      if (auditRecordId) {
        const existingIndex = chatState.messages.value.findIndex(existing => getAuditConfirmationRecordId(existing) === auditRecordId)
        if (existingIndex !== -1) {
          chatState.messages.value[existingIndex] = mergeRemoteMessage(chatState.messages.value[existingIndex], message)
          getMessageDedupeKeys(message).forEach(key => existingKeys.add(key))
          continue
        }
      }
      const replacementIndex = findMessageReplacementIndex(chatState.messages.value, message)
      if (replacementIndex !== -1) {
        chatState.messages.value[replacementIndex] = mergeRemoteMessage(chatState.messages.value[replacementIndex], message)
        getMessageDedupeKeys(message).forEach(key => existingKeys.add(key))
        continue
      }
      const transientIndex = findTransientHistoryMessageIndex(chatState.messages.value, message)
      if (transientIndex !== -1) {
        const localMessage = chatState.messages.value[transientIndex]
        chatState.messages.value[transientIndex] = mergeRemoteMessage(localMessage, message)
        getMessageDedupeKeys(message).forEach(key => existingKeys.add(key))
        continue
      }
      const messageKeys = getMessageDedupeKeys(message)
      if ([...messageKeys].some(key => existingKeys.has(key))) continue
      newMessages.push(message)
      messageKeys.forEach(key => existingKeys.add(key))
    }
    if (newMessages.length) {
      chatState.messages.value.push(...newMessages)
    }
  }

  const applyAuditConfirmationStatus = (data) => {
    if (shouldIgnoreExternalSessionEvent(sessionEventSequenceBySession, data, sessionManager.currentSessionId.value)) return
    if (!data || data.session_id && data.session_id !== sessionManager.currentSessionId.value) return
    const auditRecordId = String(data.audit_record_id || '')
    const messageIndex = chatState.messages.value.findIndex((message) => {
      if (data.message_id && String(message.db_id || message.id) === String(data.message_id)) return true
      if (message.type !== 'audit_confirmation') return false
      try {
        const payload = typeof message.content === 'string' ? JSON.parse(message.content) : message.content
        return auditRecordId && String(payload?.audit_record_id || '') === auditRecordId
      } catch {
        return false
      }
    })
    if (messageIndex !== -1) {
      const message = chatState.messages.value[messageIndex]
      const currentPayload = normalizeMessageContent(message.content)
      const incomingPayload = normalizeMessageContent(data.content)
      const mergedPayload = {
        ...(currentPayload && typeof currentPayload === 'object' ? currentPayload : {}),
        ...(incomingPayload && typeof incomingPayload === 'object' ? incomingPayload : {}),
        ...(data.status ? { status: data.status } : {})
      }
      chatState.messages.value[messageIndex] = mergeRemoteMessage(message, {
        ...message,
        id: data.message_id || message.id,
        db_id: data.message_id || message.db_id,
        type: 'audit_confirmation',
        content: JSON.stringify(mergedPayload)
      })
    }

    if (messageIndex === -1) {
      void mergeLatestSessionHistory(data.session_id || sessionManager.currentSessionId.value).catch(err => {
        console.error('Audit confirmation history merge failed:', err)
      })
    }

    if (Array.isArray(data.tool_results) || data.tool_result) {
      applyAuditToolResultsUpdate({
        ...data,
        messages: Array.isArray(data.tool_results) ? data.tool_results : [data.tool_result]
      }, { skipSequenceGuard: true })
    }
  }

  const applyAuditToolResultsUpdate = (data, { skipSequenceGuard = false } = {}) => {
    if (!skipSequenceGuard && shouldIgnoreExternalSessionEvent(sessionEventSequenceBySession, data, sessionManager.currentSessionId.value)) return
    if (!data || data.session_id && data.session_id !== sessionManager.currentSessionId.value) return
    const messages = Array.isArray(data.messages) ? data.messages : []
    for (const remoteMessage of messages) {
      if (!remoteMessage || typeof remoteMessage !== 'object') continue
      const normalizedMessage = normalizeHistoryMessage({
        ...remoteMessage,
        db_id: remoteMessage.db_id ?? remoteMessage.id
      })
      chatState.messages.value = mergeRemoteMessageIntoList(chatState.messages.value, normalizedMessage)
    }

    void mergeLatestSessionHistory(data.session_id || sessionManager.currentSessionId.value).catch(err => {
      console.error('Audit tool result history merge failed:', err)
    })
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
   * 连续发送时先插入用户消息，再直接发送。
   * queued 状态由服务端 input_queued 事件设置。
   */
  const enqueueMessage = (text, attachments = []) => {
    if (rejectReadOnlySession()) return

    const userMsgId = Date.now() + Math.random()
    
    // 添加用户消息到界面
    const attachmentsToSent = attachments.map(a => a.path)
    chatState.addMessage({
      id: userMsgId,
      role: 'user',
      content: text,
      attachments: attachmentsToSent,
      created_at: Date.now() / 1000
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
    
    // 如果没有现成的消息 ID（非队列来的），则添加用户消息
    const requestId = `req_${userMsgId}_${Math.random().toString(36).substr(2, 4)}`
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
      // 请求 ID 必须在服务端生命周期事件抵达前写入消息。
      const queuedMessage = chatState.messages.value.find(m => m.id === existingMsgId)
      if (queuedMessage) queuedMessage.request_id = requestId
    }
    chatState.loading.value = true
    nextTick(() => chatState.scrollToBottom())

    await performHttpSend(text, attachmentsToSent, userMsgId, sessionManager.currentSessionId.value, requestId)
  }

  /**
   * 实际执行 HTTP 请求（支持自动二次请求）
   */
  const performHttpSend = async (text, attachmentsToSent = [], userMsgId = null, requestSessionId = null, requestId = null) => {
    const isCurrentRequestSession = () => requestSessionId === sessionManager.currentSessionId.value
    try {
      const response = await transport.httpSend({
        message: text,
        sessionId: requestSessionId,
        attachments: attachmentsToSent,
        requestId,
        callbacks: {
          ...createLifecycleCallbacks(isCurrentRequestSession),
          onContextSummaryStart: (data) => {
            if (isCurrentRequestSession()) {
              if (!shouldIgnoreExternalSessionEvent(sessionEventSequenceBySession, data, sessionManager.currentSessionId.value)) {
                startContextSummaryWork(contextSummaryWorkKeys.value, contextSummaryRequestKeys, data, requestId)
              }
            }
          },
          onContextSummaryEnd: (data) => {
            if (isCurrentRequestSession() && !shouldIgnoreExternalSessionEvent(sessionEventSequenceBySession, data, sessionManager.currentSessionId.value)) {
              endContextSummaryWork(contextSummaryWorkKeys.value, contextSummaryRequestKeys, data, requestId)
            }
          }
        }
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
        return performHttpSend(text, attachmentsToSent, userMsgId, newId, requestId)
      }

      if (requestSessionId !== sessionManager.currentSessionId.value) return

      const auditConfirmation = parseAuditConfirmationResponse(response)
      messageProcessor.processAiResponse(chatState.messages, response, null, requestId)
      if (auditConfirmation) {
        chatState.loading.value = false
      }

      if (response.has_background_tasks) {
        void pollBackgroundTasksUntilSettled(requestSessionId, response.background_task_poll_interval || 2).catch(err => {
          console.error('Background task polling failed:', err)
        })
      }
    } catch (err) {
      if (requestSessionId !== sessionManager.currentSessionId.value) return
      finishRequestLifecycle(requestId, isCurrentRequestSession)
      ElMessage.error(err.message || t('chat.send_failed'))
    } finally {
      clearContextSummaryRequest(contextSummaryWorkKeys.value, contextSummaryRequestKeys, requestId)
      if (requestSessionId === sessionManager.currentSessionId.value) {
        try {
          await mergeLatestSessionHistory(requestSessionId)
        } catch (err) {
          console.error('HTTP response history refresh failed:', err)
        }
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
      // 请求 ID 必须在服务端生命周期事件抵达前写入消息。
      const msg = chatState.messages.value.find(m => m.id === existingMsgId)
      if (msg) {
        msg.request_id = requestId
      }
    }
    chatState.loading.value = true
    nextTick(() => chatState.scrollToBottom())

    let requestSessionId = sessionManager.currentSessionId.value
    const isCurrentRequestSession = () => requestSessionId === sessionManager.currentSessionId.value

    // 直接包装需要传递给 transport.wsSend 的 callbacks 选项
    const callbacks = {
      ...createLifecycleCallbacks(isCurrentRequestSession),
      onContextSummaryStart: (data) => {
        if (isCurrentRequestSession() && !shouldIgnoreExternalSessionEvent(sessionEventSequenceBySession, data, sessionManager.currentSessionId.value)) {
          startContextSummaryWork(contextSummaryWorkKeys.value, contextSummaryRequestKeys, data, requestId)
        }
      },
      onContextSummaryEnd: (data) => {
        if (isCurrentRequestSession() && !shouldIgnoreExternalSessionEvent(sessionEventSequenceBySession, data, sessionManager.currentSessionId.value)) {
          endContextSummaryWork(contextSummaryWorkKeys.value, contextSummaryRequestKeys, data, requestId)
        }
      },
      onContent: (text, turn, thinkingIdParam, finishReason, responseId, requestIdParam, workId, eventId) => {
        if (!isCurrentRequestSession()) return
        messageProcessor.processStreamContent(chatState.messages, text, turn, null, finishReason, responseId, requestIdParam, workId, eventId)
      },
      onToolStart: (toolCall, thinkingIdParam, responseId, requestIdParam, workId) => {
        if (!isCurrentRequestSession()) return
        messageProcessor.processStreamToolStart(chatState.messages, toolCall, null, responseId, requestIdParam, workId)
       },
       onToolEnd: (toolEnd, responseId, requestIdParam, workId) => {
         if (!isCurrentRequestSession()) return
         messageProcessor.processStreamToolEnd(chatState.messages, toolEnd, responseId, requestIdParam, workId)
      },
      onError: (errorMessage, thinkingIdParam, requestIdParam, errorData = {}) => {
        clearContextSummaryRequest(contextSummaryWorkKeys.value, contextSummaryRequestKeys, requestIdParam || requestId)
        if (!isCurrentRequestSession()) return
        const inserted = messageProcessor.processStreamError(
          chatState.messages,
          errorMessage,
          null,
          requestIdParam || requestId,
          errorData.work_id,
          errorData.event_id
        )
        if (inserted) {
          ElMessage.error(errorMessage || t('chat.stream_error'))
        }
      },
      onProactiveReply: (data) => {
        if (data.session_id && data.session_id !== sessionManager.currentSessionId.value) return
        const workId = data.work_id
        if (
          data.source === 'foreground' &&
          workId !== undefined &&
          workId !== null &&
          workId !== '' &&
          chatState.messages.value.some(message =>
            message.work_id !== undefined &&
            message.work_id !== null &&
            message.work_id !== '' &&
            String(message.work_id) === String(workId)
          )
        ) return
        void mergeLatestSessionHistory(data.session_id || sessionManager.currentSessionId.value).catch(err => {
          console.error('Proactive reply history merge failed:', err)
        })
      },
      onProactiveReplyError: (data) => {
        if (data.session_id && data.session_id !== sessionManager.currentSessionId.value) return
        const errorMessage = data.content || data.message || 'Background proactive reply failed'
        const inserted = messageProcessor.processStreamError(
          chatState.messages,
          errorMessage,
          null,
          null,
          data.work_id,
          data.event_id
        )
        if (inserted) {
          ElMessage.error(errorMessage)
        }
        void mergeLatestSessionHistory(data.session_id || sessionManager.currentSessionId.value).catch(err => {
          console.error('Proactive reply error history merge failed:', err)
        })
      },
      onAuditConfirmationStatus: applyAuditConfirmationStatus,
      onAuditToolResultsUpdate: applyAuditToolResultsUpdate,
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
        if (eventType !== 'turn_end') {
          clearContextSummaryRequest(contextSummaryWorkKeys.value, contextSummaryRequestKeys, requestIdParam || requestId)
        }
        if (!isCurrentRequestSession()) return
        if (data.session_id && data.session_id !== requestSessionId) return

        // 每个 response_id 只保留一条正文；工具轮次的正文归入工具消息
        if (eventType === 'turn_end') {
          if (data.response_id) {
            const matchingIndexes = chatState.messages.value
              .map((message, index) => ({ message, index }))
              .filter(item => item.message.response_id === data.response_id && item.message.role === 'assistant')
            const toolItem = matchingIndexes.find(item => isToolCall(item.message))
            const plainItems = matchingIndexes.filter(item => !isToolCall(item.message))
            const targetItem = toolItem || plainItems[0]

            if (targetItem) {
              const targetMessage = targetItem.message
              const updatedMessage = data.content === undefined || data.content === null
                ? { ...targetMessage, work_id: targetMessage.work_id || data.work_id }
                : toolItem
                  ? {
                      ...targetMessage,
                      content: JSON.stringify({
                        ...normalizeMessageContent(targetMessage.content),
                        content: data.content
                      }),
                      work_id: targetMessage.work_id || data.work_id
                    }
                  : { ...targetMessage, content: data.content, work_id: targetMessage.work_id || data.work_id }
              const duplicateIndexes = new Set(
                matchingIndexes
                  .filter(item => item.index !== targetItem.index)
                  .map(item => item.index)
              )
              chatState.messages.value = chatState.messages.value
                .map((message, index) => index === targetItem.index ? updatedMessage : message)
                .filter((_, index) => !duplicateIndexes.has(index))
            } else if (typeof data.content === 'string' && data.content) {
              messageProcessor.processStreamContent(
                chatState.messages,
                data.content,
                data.turn,
                null,
                null,
                data.response_id,
                requestIdParam,
                data.work_id,
                data.event_id
              )
            }
          }
          return // turn_end 时不需要执行 done 的历史比对和占位符清理
        }

        const completedResponse = data.response || data
        const auditConfirmation = parseAuditConfirmationResponse(completedResponse)
        if (auditConfirmation) {
          const auditRecordId = String(auditConfirmation.audit_record_id || '')
          const existingIndex = chatState.messages.value.findIndex(message => getAuditConfirmationRecordId(message) === auditRecordId)
          if (existingIndex === -1) {
            messageProcessor.processAiResponse(chatState.messages, completedResponse, null, requestIdParam)
          } else {
            const existingMessage = chatState.messages.value[existingIndex]
            chatState.messages.value[existingIndex] = {
              ...existingMessage,
              type: 'audit_confirmation',
              content: JSON.stringify(auditConfirmation),
              request_id: existingMessage.request_id || requestIdParam
            }
          }
          chatState.loading.value = false
          return
        }

        // 已确认工具执行通过独立 done 返回完整正文，不会再发送 content 增量事件。
        if (!Array.isArray(completedResponse?.choices) && typeof completedResponse?.content === 'string' && completedResponse.content) {
          const requestMessageIndex = requestIdParam
            ? chatState.messages.value.findLastIndex(message => message.role === 'user' && message.request_id === requestIdParam)
            : -1
          const existingFinalIndex = chatState.messages.value.findLastIndex((message, index) =>
            index > requestMessageIndex &&
            message.role === 'assistant' &&
            message.type !== 'audit_confirmation' &&
            !isToolCall(message) &&
            JSON.stringify(normalizeMessageContent(message.content)) === JSON.stringify(normalizeMessageContent(completedResponse.content))
          )

          if (existingFinalIndex !== -1) {
            const existingFinal = chatState.messages.value[existingFinalIndex]
            chatState.messages.value[existingFinalIndex] = {
              ...existingFinal,
              request_id: existingFinal.request_id || requestIdParam,
              work_id: existingFinal.work_id || completedResponse.work_id || data.work_id,
              response_id: existingFinal.response_id || completedResponse.response_id || data.response_id
            }
          } else {
            messageProcessor.processAiResponse(
              chatState.messages,
              {
                ...completedResponse,
                work_id: completedResponse.work_id || data.work_id,
                response_id: completedResponse.response_id || data.response_id
              },
              null,
              requestIdParam
            )
          }
        }

        if (data.files && data.files.length > 0) {
          const newMessages = [...chatState.messages.value]
          let targetIdx = -1

          if (data.response_id) {
            targetIdx = newMessages.findLastIndex(m => m.response_id === data.response_id && m.role === 'assistant' && !isToolCall(m))
          }

          if (targetIdx === -1 && data.work_id) {
            targetIdx = newMessages.findLastIndex(m => m.work_id === data.work_id && m.role === 'assistant' && !isToolCall(m))
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
            messageProcessor.processAiResponse(chatState.messages, data.response || data, null, requestIdParam)
          }
        }

      },
      setLoading: (val) => {
        if (!isCurrentRequestSession()) return
        if (!val && chatState.messages.value.some(message => message.role === 'thinking')) return
        chatState.loading.value = val
      }
    }
    
    const handleWsSendFailure = () => {
      clearContextSummaryRequest(contextSummaryWorkKeys.value, contextSummaryRequestKeys, requestId)
      if (!isCurrentRequestSession()) return
      finishRequestLifecycle(requestId, isCurrentRequestSession)
      ElMessage.error(t('chat.ws_send_failed'))
      if (!chatState.messages.value.some(message => message.role === 'thinking')) {
        chatState.loading.value = false
      }
      transport.setTransportMode('http')
    }

    try {
      const sent = await transport.wsSend({
        message: text,
        sessionId: requestSessionId,
        attachments: attachmentsToSent,
        requestId,
        callbacks
      })
      if (!sent) handleWsSendFailure()
    } catch (e) {
      console.error('WebSocket发送失败:', e)
      handleWsSendFailure()
    }
  }

  // ==================== 会话选择 ====================
  
  // 选择会话；session 为会话对象
  const selectSession = (session) => {
    clearAllContextSummaryWorks(contextSummaryWorkKeys.value, contextSummaryRequestKeys)
    chatState.messages.value = resetWorkLifecycle(chatState.messages.value)
    initialHistoryLoaded.value = false
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
    clearAllContextSummaryWorks(contextSummaryWorkKeys.value, contextSummaryRequestKeys)
    chatState.messages.value = resetWorkLifecycle(chatState.messages.value)
    initialHistoryLoaded.value = true
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
  const handleScroll = async () => {
    const messageList = chatState.messageList.value
    if (!messageList || restoringHistoryScroll || chatState.messages.value.length === 0 || !sessionManager.hasMore.value || sessionManager.historyLoading.value) return
    if (messageList.scrollTop > 500) return

    const historyData = await sessionManager.loadSessionHistory(1)
    if (!historyData?.length) return

    const existingKeys = new Set(chatState.messages.value.flatMap(message => [...getMessageDedupeKeys(message)]))
    const uniqueMessages = historyData
      .map(normalizeHistoryMessage)
      .filter((message) => {
        const messageKeys = getMessageDedupeKeys(message)
        if ([...messageKeys].some(key => existingKeys.has(key))) return false
        messageKeys.forEach(key => existingKeys.add(key))
        return true
      })
    if (!uniqueMessages.length) return

    const anchor = messageList.captureScrollAnchor()
    restoringHistoryScroll = true
    try {
      chatState.insertMessage(0, uniqueMessages)
      await nextTick()
      await messageList.restoreScrollAnchor(anchor)
    } finally {
      requestAnimationFrame(() => {
        restoringHistoryScroll = false
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
    isContextSummarizing,
    initialHistoryLoaded,
    
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
    historyLoading: sessionManager.historyLoading,
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
    getToolCalls,
    getToolCallName,
    getToolCallArguments,
    getToolCallContent,
    getToolResultName,
    getToolResultContent,
    getMessageTimestamp,
    
    // 滚动事件
    handleScroll,
    bindScrollEvent,
    unbindScrollEvent
  }
}
