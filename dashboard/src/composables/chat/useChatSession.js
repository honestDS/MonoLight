// 聊天会话管理 composable，聚合状态、会话、通信与消息处理
import { computed, nextTick, onScopeDispose, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useChatState } from './useChatState'
import { useSessionManager } from './useSessionManager'
import { useChatTransport } from './useChatTransport'
import { resolveAssistantDisplayContent, useMessageProcessor } from './useMessageProcessor'
import { createContextSummaryTracker } from './contextSummaryTracker.js'
import { createHistoryMergeTracker } from './historyMergeTracker.js'
import { applyResumedTurnEnd, createSessionReconnectHandler, resumeSessionStream, getInitialResumeLoading } from './streamResume.js'
import { withSessionActivity } from './sessionActivity.js'
import { createWorkLifecycleTracker, shouldApplyOwnProactiveReply } from './workLifecycleTracker.js'
import { applyAuditConfirmationStatusToMessages, applyAuditToolResultsUpdateToMessages } from './auditConfirmationState.js'
import { hasHttpResultMessage, shouldFetchHttpWorkStatus } from './sessionListLoading.js'
import { createHttpHistorySyncController } from './httpHistorySync.js'
import { persistSessionTransportMode, resolveSessionTransportMode, resumeSelectedSessionByTransport } from './sessionTransportMode.js'
import { findAssistantResponseReplacementIndex, findMessageReplacementIndex, formatTimestamp, getMessageDedupeKeys, getMessageTimestamp, getToolCallArguments, getToolCallContent, getToolCallName, getToolCalls, getToolResultContent, getToolResultName, isAssistantResponse, isPlainAssistantResponse, isToolCall, isToolResult, mergeAssistantResponseIntoList, mergeRemoteMessage, normalizeMessageContent } from '../../utils'
import { getNewSessionProfileOverrideId } from '../../utils/profileOptions'
import {
  mergeTodoPlan,
  normalizeTodoPlan,
  readTodoPlanFromTransport
} from '../../utils/todoPresentation.js'
import { filterResponseHistoryToolOutput, filterToolOutputMessages } from '../../utils/toolOutputVisibility'
import { shouldReturnToWelcomeAfterSessionDelete } from '../../utils/chatContentReveal.js'
import { chatApi } from '../../api'
import i18n from '../../i18n'
import { truncateErrorMessage } from '../../utils/errorMessage.js'

const t = (key, ...args) => i18n.global.t(key, ...args)
const HTTP_HISTORY_FAST_SYNC_INTERVAL_MS = 2000

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
  for (const content of [response?.choices?.[0]?.message?.content, response?.content]) {
    try {
      const payload = typeof content === 'string' ? JSON.parse(content) : content
      if (payload?.type === 'audit_confirmation') return payload
    } catch {}
  }
  return null
}

const normalizeLlmRequestMetadata = (metadata) => {
  const tokenFields = ['input_tokens', 'context_window_tokens', 'max_output_tokens']
  if (!tokenFields.every(field => Number.isFinite(metadata?.[field]) && metadata[field] >= 0)) return null

  const normalizedMetadata = {
    input_tokens: Math.trunc(metadata.input_tokens),
    context_window_tokens: Math.trunc(metadata.context_window_tokens),
    max_output_tokens: Math.trunc(metadata.max_output_tokens)
  }
  for (const field of ['output_tokens', 'cached_tokens', 'total_output_tokens']) {
    if (!Object.prototype.hasOwnProperty.call(metadata, field)) continue
    if (!Number.isFinite(metadata[field]) || metadata[field] < 0) return null
    normalizedMetadata[field] = Math.trunc(metadata[field])
  }
  if (Object.prototype.hasOwnProperty.call(metadata, 'cache_hit_rate')) {
    if (!Number.isFinite(metadata.cache_hit_rate) || metadata.cache_hit_rate < 0 || metadata.cache_hit_rate > 1) return null
    normalizedMetadata.cache_hit_rate = Number(metadata.cache_hit_rate)
  }
  if (Object.prototype.hasOwnProperty.call(metadata, 'response_id')) normalizedMetadata.response_id = metadata.response_id
  if (Object.prototype.hasOwnProperty.call(metadata, 'turn')) normalizedMetadata.turn = metadata.turn
  if (Object.prototype.hasOwnProperty.call(metadata, 'work_sequence_no')) {
    if (!Number.isFinite(metadata.work_sequence_no) || !Number.isInteger(metadata.work_sequence_no) || metadata.work_sequence_no <= 0) return null
    normalizedMetadata.work_sequence_no = metadata.work_sequence_no
  }
  if (Object.prototype.hasOwnProperty.call(metadata, 'event_sequence_no')) {
    if (!Number.isFinite(metadata.event_sequence_no) || !Number.isInteger(metadata.event_sequence_no) || metadata.event_sequence_no < 0) return null
    normalizedMetadata.event_sequence_no = metadata.event_sequence_no
  }
  return normalizedMetadata
}

const shouldReplaceLlmRequestMetadata = (currentMetadata, incomingMetadata) => {
  if (!Object.prototype.hasOwnProperty.call(incomingMetadata, 'work_sequence_no')) return true
  if (!Object.prototype.hasOwnProperty.call(currentMetadata || {}, 'work_sequence_no')) return true
  if (incomingMetadata.work_sequence_no !== currentMetadata.work_sequence_no) {
    return incomingMetadata.work_sequence_no > currentMetadata.work_sequence_no
  }
  return (incomingMetadata.event_sequence_no ?? 0) >= (currentMetadata.event_sequence_no ?? 0)
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
  const contextSummaryTracker = createContextSummaryTracker()
  const llmRequestMetadataBySession = ref(new Map())
  const workLifecycleTracker = createWorkLifecycleTracker()
  const historyMergeTracker = createHistoryMergeTracker()
  const initialHistoryLoaded = ref(true)
  const pendingHttpRequests = new Map()
  const observedHttpWorkStatuses = new Map()
  const observedHttpLatestMessageIds = new Map()
  const fetchingHttpWorks = new Set()
  const resolvedHttpWorks = new Set()
  let httpPollingStateVersion = 0

  const normalizeHttpIdentity = value => (
    value === undefined || value === null || value === '' ? null : String(value)
  )

  const trackHttpSubmission = (requestId, sessionId, workId = null) => {
    const normalizedRequestId = normalizeHttpIdentity(requestId)
    const normalizedSessionId = normalizeHttpIdentity(sessionId)
    if (!normalizedRequestId || !normalizedSessionId) return

    const previous = pendingHttpRequests.get(normalizedRequestId)
    pendingHttpRequests.set(normalizedRequestId, {
      sessionId: normalizedSessionId,
      workId: normalizeHttpIdentity(workId) || previous?.workId || null
    })
  }

  const resetHttpPollingState = () => {
    httpPollingStateVersion += 1
    pendingHttpRequests.clear()
    observedHttpWorkStatuses.clear()
    observedHttpLatestMessageIds.clear()
    fetchingHttpWorks.clear()
    resolvedHttpWorks.clear()
  }
  
  // 默认 Markdown 开关状态（用于未选择会话时）
  const enableMarkdownDefault = ref(false)
  const showToolCallsDefault = ref(true)
  const showReasoningDefault = ref(true)
  const newSessionProfileOverrideId = ref(null)
  
  // 2. 会话管理
  const sessionManager = useSessionManager()
  const currentTodoPlan = ref(null)
  const skipTodoInitialLoadSessionIds = new Set()
  let todoLoadVersion = 0

  const applyTodoPlan = (plan, sessionId = sessionManager.currentSessionId.value) => {
    if (!sessionId || sessionId !== sessionManager.currentSessionId.value) return
    currentTodoPlan.value = mergeTodoPlan(currentTodoPlan.value, plan)
  }

  const applyTodoTransportPayload = (payload, sessionId = payload?.session_id || sessionManager.currentSessionId.value) => {
    const plan = readTodoPlanFromTransport(payload)
    if (plan) applyTodoPlan(plan, sessionId)
  }

  const markNewSessionTodoKnownEmpty = (sessionId) => {
    if (sessionId) skipTodoInitialLoadSessionIds.add(sessionId)
  }

  watch(
    () => sessionManager.currentSessionId.value,
    async (sessionId) => {
      const requestVersion = ++todoLoadVersion
      currentTodoPlan.value = null
      if (!sessionId) return
      if (skipTodoInitialLoadSessionIds.delete(sessionId)) {
        currentTodoPlan.value = normalizeTodoPlan(null)
        return
      }
      try {
        const response = await chatApi.sessionTodo(sessionId)
        if (requestVersion !== todoLoadVersion || sessionId !== sessionManager.currentSessionId.value) return
        applyTodoPlan(response.data?.data, sessionId)
      } catch {
        if (requestVersion === todoLoadVersion && sessionId === sessionManager.currentSessionId.value) {
          currentTodoPlan.value = null
        }
      }
    },
    { immediate: true }
  )
  const isContextSummarizing = computed(() => contextSummaryWorkKeys.value.size > 0)
  const currentSession = computed(() =>
    sessionManager.sessions.value.find(
      session => session.session_id === sessionManager.currentSessionId.value
    ) || null
  )
  const currentSessionShowToolCalls = computed({
    get: () => {
      if (!sessionManager.currentSessionId.value) return showToolCallsDefault.value
      return currentSession.value?.show_tool_calls ?? true
    },
    set: (showToolCalls) => {
      const enabled = Boolean(showToolCalls)
      const sessionId = sessionManager.currentSessionId.value
      if (!sessionId) {
        showToolCallsDefault.value = enabled
        return
      }

      const sessionIndex = sessionManager.sessions.value.findIndex(session => session.session_id === sessionId)
      if (sessionIndex !== -1) {
        sessionManager.sessions.value[sessionIndex] = {
          ...sessionManager.sessions.value[sessionIndex],
          show_tool_calls: enabled
        }
      }
    }
  })
  const currentSessionShowReasoning = computed({
    get: () => {
      if (!sessionManager.currentSessionId.value) return showReasoningDefault.value
      return currentSession.value?.show_reasoning ?? true
    },
    set: (showReasoning) => {
      const enabled = Boolean(showReasoning)
      const sessionId = sessionManager.currentSessionId.value
      if (!sessionId) {
        showReasoningDefault.value = enabled
        return
      }

      const sessionIndex = sessionManager.sessions.value.findIndex(session => session.session_id === sessionId)
      if (sessionIndex !== -1) {
        sessionManager.sessions.value[sessionIndex] = {
          ...sessionManager.sessions.value[sessionIndex],
          show_reasoning: enabled
        }
      }
    }
  })
  const llmRequestMetadata = computed(() => {
    const sessionId = sessionManager.currentSessionId.value
    if (!sessionId) return null
    return llmRequestMetadataBySession.value.get(sessionId)
      || normalizeLlmRequestMetadata(currentSession.value?.llm_request_metadata)
  })
  const isCurrentSessionReadOnly = computed(() => {
    const source = currentSession.value?.source
    return Boolean(source && !['http', 'ws'].includes(source))
  })

  // 3. 通信层
  const transport = useChatTransport()
  const modeSettingSubmitting = ref(false)
  const transportFallbackNotice = ref('')
  const transportModeChangeBlocked = computed(() => (
    Boolean(chatState.loading.value || currentSession.value?.is_loading)
  ))
  const clearTransportFallbackNotice = () => {
    transportFallbackNotice.value = ''
  }

  const setTransportMode = async (mode, { notifyError = true } = {}) => {
    if (modeSettingSubmitting.value) return false

    const sessionId = sessionManager.currentSessionId.value
    if (!sessionId) {
      await transport.setTransportMode(mode, transport.disconnectWebSocket)
      clearTransportFallbackNotice()
      return true
    }
    if (isCurrentSessionReadOnly.value) return false

    const previousSession = sessionManager.sessions.value.find(session => session.session_id === sessionId)
    const previousMode = previousSession?.source
    if (previousMode === mode) return true

    modeSettingSubmitting.value = true
    try {
      await persistSessionTransportMode({
        sessionId,
        mode,
        sessions: sessionManager.sessions.value,
        getCurrentSessionId: () => sessionManager.currentSessionId.value,
        getCurrentTransportMode: () => transport.transportMode.value,
        updateSessionSetting: (targetSessionId, payload) => chatApi.updateSessionSetting(targetSessionId, payload),
        applyTransportMode: targetMode => transport.setTransportMode(targetMode, transport.disconnectWebSocket)
      })
      clearTransportFallbackNotice()
      return true
    } catch (error) {
      if (notifyError) ElMessage.error(error.message || t('chat.setting_failed'))
      return false
    } finally {
      modeSettingSubmitting.value = false
    }
  }

  // 4. 消息处理
  const messageProcessor = useMessageProcessor()
  const filterNewMessages = (messages) => filterToolOutputMessages(
    messages,
    currentSessionShowToolCalls.value
  )
  const processAiResponse = (response, thinkingId = null, requestId = null) => {
    messageProcessor.processAiResponse(
      chatState.messages,
      filterResponseHistoryToolOutput(response, currentSessionShowToolCalls.value),
      thinkingId,
      requestId
    )
  }
  const selectNewSession = (session) => {
    historyMergeTracker.invalidate()
    const activeSession = withSessionActivity(session)
    const sessionIndex = sessionManager.sessions.value.findIndex(item => item.session_id === activeSession.session_id)
    if (sessionIndex === -1) {
      sessionManager.sessions.value.unshift(activeSession)
    } else {
      sessionManager.sessions.value[sessionIndex] = {
        ...sessionManager.sessions.value[sessionIndex],
        ...activeSession
      }
    }
    sessionManager.selectSession(activeSession, null, false, false)
  }

  const applyLifecycleEvent = (updateMessages, event, isCurrentRequestSession) => {
    if (!isCurrentRequestSession()) return
    const currentSessionId = sessionManager.currentSessionId.value
    if (event?.session_id && currentSessionId && event.session_id !== currentSessionId) return

    chatState.messages.value = updateMessages(chatState.messages.value, event)
    void nextTick(() => {
      if (isCurrentRequestSession()) chatState.followOutputToBottom('auto')
    })
  }

  const updateLlmRequestMetadata = (event, isCurrentRequestSession) => {
    if (!isCurrentRequestSession()) return
    const currentSessionId = sessionManager.currentSessionId.value
    const sessionId = event?.session_id || currentSessionId
    if (!sessionId || sessionId !== currentSessionId) return

    const metadata = normalizeLlmRequestMetadata(event)
    if (!metadata) return

    const sessionIndex = sessionManager.sessions.value.findIndex(session => session.session_id === sessionId)
    const currentMetadata = llmRequestMetadataBySession.value.get(sessionId)
      || normalizeLlmRequestMetadata(sessionManager.sessions.value[sessionIndex]?.llm_request_metadata)
    if (!shouldReplaceLlmRequestMetadata(currentMetadata, metadata)) return

    for (const field of ['output_tokens', 'cached_tokens', 'cache_hit_rate', 'total_output_tokens']) {
      if (!Object.prototype.hasOwnProperty.call(metadata, field) && Object.prototype.hasOwnProperty.call(currentMetadata || {}, field)) {
        metadata[field] = currentMetadata[field]
      }
    }

    const nextMetadataBySession = new Map(llmRequestMetadataBySession.value)
    nextMetadataBySession.set(sessionId, metadata)
    llmRequestMetadataBySession.value = nextMetadataBySession

    if (sessionIndex !== -1) {
      sessionManager.sessions.value[sessionIndex] = {
        ...sessionManager.sessions.value[sessionIndex],
        llm_request_metadata: metadata
      }
    }
  }

  const refreshSessionLoadingState = () => {
    void sessionManager.refreshSessionLoadingState()
  }

  const createLifecycleCallbacks = isCurrentRequestSession => ({
    onInputAccepted: () => {
      refreshSessionLoadingState()
      if (transportFallbackNotice.value === t('chat.ws_submission_unknown')) {
        clearTransportFallbackNotice()
      }
    },
    onInputQueued: event => {
      refreshSessionLoadingState()
      applyLifecycleEvent(workLifecycleTracker.markInputQueued, event, isCurrentRequestSession)
    },
    onInputDequeued: event => applyLifecycleEvent(workLifecycleTracker.markInputsDequeued, event, isCurrentRequestSession),
    onAgentLoopStart: event => applyLifecycleEvent(workLifecycleTracker.startAgentLoop, event, isCurrentRequestSession),
    onAgentLoopOutput: event => {
      // 跨过一次实际绘制，确保 agent_loop_start 创建的 thinking 已显示一帧。
      requestAnimationFrame(() => requestAnimationFrame(() => {
        applyLifecycleEvent(workLifecycleTracker.stopAgentLoop, event, isCurrentRequestSession)
      }))
    },
    onLlmRequestMetadata: event => updateLlmRequestMetadata(event, isCurrentRequestSession),
    onTodoUpdate: event => {
      if (isCurrentRequestSession()) applyTodoTransportPayload(event)
    },
    onWorkFinished: event => {
      refreshSessionLoadingState()
      applyLifecycleEvent(workLifecycleTracker.finishWorkLifecycle, event, isCurrentRequestSession)
    }
  })

  const finishRequestLifecycle = (requestId, isCurrentRequestSession) => {
    if (!requestId) return
    applyLifecycleEvent(
      workLifecycleTracker.finishWorkLifecycle,
      { request_ids: [requestId] },
      isCurrentRequestSession
    )
  }

  const getCompletedWorkId = (data) => data?.work_id ?? data?.response?.work_id

  const shouldProcessCompletedWork = (data) => {
    const workId = getCompletedWorkId(data)
    return !workLifecycleTracker.isWorkTerminal(workId)
      || workLifecycleTracker.isAcceptedTerminalEvent(data)
  }

  const rejectReadOnlySession = () => {
    if (!isCurrentSessionReadOnly.value) return false
    ElMessage.warning(t('chat.external_session_read_only'))
    return true
  }

  let restoringHistoryScroll = false

  // ==================== 设置模块间连接 ====================
  
  const loadInitialSessionHistory = async (pageCount) => {
    const requestedSessionId = sessionManager.currentSessionId.value
    const historyData = await sessionManager.loadSessionHistory(pageCount)
    if (requestedSessionId !== sessionManager.currentSessionId.value) return

    restoringHistoryScroll = true
    try {
      const visibleHistoryData = filterNewMessages(historyData)
      if (visibleHistoryData.length > 0) {
        // 插入到消息列表开头
        chatState.insertMessage(0, visibleHistoryData.map(normalizeHistoryMessage), true)
      }
      initialHistoryLoaded.value = true
      await nextTick()
      if (visibleHistoryData.length > 0) {
        await chatState.scrollToBottom('auto')
      }
    } finally {
      requestAnimationFrame(() => {
        restoringHistoryScroll = false
      })
    }
    return historyData
  }

  // 设置会话管理的历史记录加载回调
  sessionManager.setLoadHistoryCallback((pageCount) => loadInitialSessionHistory(pageCount))

  const reloadCurrentSessionHistory = async () => {
    if (!sessionManager.currentSessionId.value) return
    initialHistoryLoaded.value = false
    chatState.clearMessages()
    sessionManager.resetPagination()
    await loadInitialSessionHistory(2)
  }

  const mergeLatestSessionHistory = async (sessionId = sessionManager.currentSessionId.value) => {
    if (!sessionId || sessionId !== sessionManager.currentSessionId.value) return
    const requestId = historyMergeTracker.begin()
    const res = await chatApi.sessionsHistory(sessionId, 1, 20)
    if (sessionId !== sessionManager.currentSessionId.value || !historyMergeTracker.isLatest(requestId)) return
    const historyData = filterNewMessages(res.data?.data || [])
    if (!historyData.length) return

    const existingKeys = new Set(chatState.messages.value.flatMap(m => [...getMessageDedupeKeys(m)]))
    let mergedMessages = [...chatState.messages.value]
    let changed = false
    for (const item of historyData) {
      const message = normalizeHistoryMessage({ ...item, db_id: item.id })
      if (isAssistantResponse(message)) {
        const replacementIndex = findAssistantResponseReplacementIndex(mergedMessages, message)
        if (replacementIndex !== -1) {
          mergedMessages = mergeAssistantResponseIntoList(mergedMessages, message)
          getMessageDedupeKeys(message).forEach(key => existingKeys.add(key))
          changed = true
          continue
        }
        if (isPlainAssistantResponse(message)) {
          mergedMessages.push(message)
          getMessageDedupeKeys(message).forEach(key => existingKeys.add(key))
          changed = true
          continue
        }
      }

      const auditRecordId = getAuditConfirmationRecordId(message)
      if (auditRecordId) {
        const existingIndex = mergedMessages.findIndex(existing => getAuditConfirmationRecordId(existing) === auditRecordId)
        if (existingIndex !== -1) {
          mergedMessages[existingIndex] = mergeRemoteMessage(mergedMessages[existingIndex], message)
          getMessageDedupeKeys(message).forEach(key => existingKeys.add(key))
          changed = true
          continue
        }
      }
      const replacementIndex = findMessageReplacementIndex(mergedMessages, message)
      if (replacementIndex !== -1) {
        mergedMessages[replacementIndex] = mergeRemoteMessage(mergedMessages[replacementIndex], message)
        getMessageDedupeKeys(message).forEach(key => existingKeys.add(key))
        changed = true
        continue
      }
      const transientIndex = findTransientHistoryMessageIndex(mergedMessages, message)
      if (transientIndex !== -1) {
        const localMessage = mergedMessages[transientIndex]
        mergedMessages[transientIndex] = mergeRemoteMessage(localMessage, message)
        getMessageDedupeKeys(message).forEach(key => existingKeys.add(key))
        changed = true
        continue
      }
      const messageKeys = getMessageDedupeKeys(message)
      if ([...messageKeys].some(key => existingKeys.has(key))) continue
      mergedMessages.push(message)
      messageKeys.forEach(key => existingKeys.add(key))
      changed = true
    }
    if (changed) {
      chatState.messages.value = mergedMessages
    }
  }

  const canSyncCurrentSessionHistory = () => !isCurrentSessionReadOnly.value && transport.transportMode.value === 'http'

  const httpHistorySync = createHttpHistorySyncController({
    getSessionId: () => sessionManager.currentSessionId.value,
    canSync: canSyncCurrentSessionHistory,
    isLoading: () => chatState.loading.value,
    fetchBackgroundTasks: sessionId => chatApi.backgroundTasks({
      session_id: sessionId,
      page: 1,
      size: 20
    }),
    mergeLatestHistory: mergeLatestSessionHistory,
    intervalMs: HTTP_HISTORY_FAST_SYNC_INTERVAL_MS,
    onError: err => {
      console.error('HTTP session history synchronization failed:', err)
    }
  })

  const stopHttpHistorySync = httpHistorySync.stop
  const startHttpHistoryBackgroundTaskSync = httpHistorySync.start

  watch(
    () => [transport.transportMode.value, sessionManager.currentSessionId.value, isCurrentSessionReadOnly.value],
    () => httpHistorySync.handleSessionChanged(),
    { immediate: true }
  )

  onScopeDispose(() => {
    stopHttpHistorySync()
    resetHttpPollingState()
    contextSummaryTracker.clearAllContextSummaryWorks(contextSummaryWorkKeys.value, contextSummaryRequestKeys)
    workLifecycleTracker.resetWorkLifecycle(chatState.messages.value)
    sessionManager.setSessionsUpdatedCallback(null)
    sessionManager.setPendingSubmissionCallback(null)
    transport.setReconnectHandler(null)
  })

  const applyAuditConfirmationStatus = (data) => {
    if (contextSummaryTracker.shouldIgnoreExternalSessionEvent(data, sessionManager.currentSessionId.value)) return
    if (!data || data.session_id && data.session_id !== sessionManager.currentSessionId.value) return
    const result = applyAuditConfirmationStatusToMessages(chatState.messages.value, data)
    if (result.updated) {
      chatState.messages.value = result.messages
    } else {
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
    if (!skipSequenceGuard && contextSummaryTracker.shouldIgnoreExternalSessionEvent(data, sessionManager.currentSessionId.value)) return
    if (!data || data.session_id && data.session_id !== sessionManager.currentSessionId.value) return
    const result = applyAuditToolResultsUpdateToMessages(
      chatState.messages.value,
      data,
      currentSessionShowToolCalls.value
    )
    if (result.updated) {
      chatState.messages.value = result.messages
    }

    void mergeLatestSessionHistory(data.session_id || sessionManager.currentSessionId.value).catch(err => {
      console.error('Audit tool result history merge failed:', err)
    })
  }

  const applyNonStreamSessionEvents = (response) => {
    const events = Array.isArray(response?.session_events) ? response.session_events : []
    for (const event of events) {
      if (event?.type === 'audit_confirmation_status') {
        applyAuditConfirmationStatus(event)
      } else if (event?.type === 'audit_tool_results_update') {
        applyAuditToolResultsUpdate(event)
      } else if (event?.type === 'todo_update') {
        applyTodoTransportPayload(event)
      }
    }
  }

  const isCurrentWritableHttpSession = sessionId => (
    transport.transportMode.value === 'http'
    && normalizeHttpIdentity(sessionManager.currentSessionId.value) === normalizeHttpIdentity(sessionId)
    && !isCurrentSessionReadOnly.value
  )

  const getPendingHttpRequestIdsForWork = (sessionId, workId, requestIds) => {
    const normalizedSessionId = normalizeHttpIdentity(sessionId)
    const normalizedWorkId = normalizeHttpIdentity(workId)
    const normalizedRequestIds = new Set(
      (Array.isArray(requestIds) ? requestIds : [])
        .map(normalizeHttpIdentity)
        .filter(Boolean)
    )
    const matchedRequestIds = new Set(normalizedRequestIds)

    for (const [requestId, pending] of pendingHttpRequests) {
      if (pending?.sessionId !== normalizedSessionId) continue
      if (pending.workId === normalizedWorkId || normalizedRequestIds.has(requestId)) {
        matchedRequestIds.add(requestId)
      }
    }

    return matchedRequestIds
  }

  const hasPendingHttpRequestForWork = (sessionId, workId, requestIds) => {
    const normalizedSessionId = normalizeHttpIdentity(sessionId)
    const normalizedWorkId = normalizeHttpIdentity(workId)
    const normalizedRequestIds = new Set(
      (Array.isArray(requestIds) ? requestIds : [])
        .map(normalizeHttpIdentity)
        .filter(Boolean)
    )

    for (const [requestId, pending] of pendingHttpRequests) {
      if (pending?.sessionId !== normalizedSessionId) continue
      if (pending.workId === normalizedWorkId || normalizedRequestIds.has(requestId)) return true
    }
    return false
  }

  const finishHttpWorkLifecycle = ({ sessionId, workId, resolvedWorkId, requestIds }) => {
    const normalizedSessionId = normalizeHttpIdentity(sessionId)
    const normalizedWorkId = normalizeHttpIdentity(workId)
    const normalizedResolvedWorkId = normalizeHttpIdentity(resolvedWorkId)
    const relatedRequestIds = getPendingHttpRequestIdsForWork(
      normalizedSessionId,
      normalizedWorkId,
      requestIds
    )
    const lifecycleWorkId = normalizedResolvedWorkId || normalizedWorkId

    if (isCurrentWritableHttpSession(normalizedSessionId)) {
      const lifecycleEvent = {
        ...(lifecycleWorkId ? { work_id: lifecycleWorkId } : {}),
        request_ids: Array.from(relatedRequestIds)
      }
      chatState.messages.value = workLifecycleTracker.finishWorkLifecycle(
        chatState.messages.value,
        lifecycleEvent
      )
      for (const requestId of relatedRequestIds) {
        chatState.messages.value = workLifecycleTracker.finishWorkLifecycle(
          chatState.messages.value,
          { request_ids: [requestId] }
        )
      }
    }

    for (const [requestId, pending] of pendingHttpRequests) {
      if (pending?.sessionId !== normalizedSessionId) continue
      if (
        relatedRequestIds.has(requestId)
        || pending.workId === normalizedWorkId
        || pending.workId === normalizedResolvedWorkId
      ) {
        pendingHttpRequests.delete(requestId)
      }
    }

    return relatedRequestIds
  }

  const maybeMergeHttpSessionHistory = (sessionId, latestMessageId) => {
    const normalizedSessionId = normalizeHttpIdentity(sessionId)
    if (
      !normalizedSessionId
      || latestMessageId === undefined
      || latestMessageId === null
      || latestMessageId === ''
      || (typeof latestMessageId === 'string' && latestMessageId.trim() === '')
      || !initialHistoryLoaded.value
      || !isCurrentWritableHttpSession(normalizedSessionId)
    ) return

    const normalizedLatestMessageId = Number(latestMessageId)
    if (!Number.isSafeInteger(normalizedLatestMessageId)) return

    const previousLatestMessageId = observedHttpLatestMessageIds.get(normalizedSessionId)
    const baselineLatestMessageId = previousLatestMessageId === undefined
      ? chatState.messages.value.reduce((maxId, message) => {
          const dbId = Number(message?.db_id)
          return Number.isSafeInteger(dbId) && dbId >= 0 ? Math.max(maxId, dbId) : maxId
        }, 0)
      : previousLatestMessageId
    if (normalizedLatestMessageId <= baselineLatestMessageId) return

    observedHttpLatestMessageIds.set(normalizedSessionId, normalizedLatestMessageId)

    void mergeLatestSessionHistory(normalizedSessionId).catch(err => {
      console.error('HTTP session list history merge failed:', err)
    })
  }

  const applyHttpWorkStatus = (work, statusData, sessionId) => {
    const workId = normalizeHttpIdentity(work?.work_id)
    const normalizedSessionId = normalizeHttpIdentity(sessionId)
    if (!workId || !statusData || !isCurrentWritableHttpSession(normalizedSessionId)) return null

    const resultSessionId = normalizeHttpIdentity(statusData.session_id)
    if (resultSessionId && resultSessionId !== normalizedSessionId) return null

    const status = String(statusData.status || '').toLowerCase()
    if (['ready_for_llm', 'running', 'waiting_external_work'].includes(status)) {
      observedHttpWorkStatuses.delete(workId)
      return { active: true }
    }
    if (!['succeeded', 'failed', 'cancelled'].includes(status)) {
      observedHttpWorkStatuses.delete(workId)
      return null
    }

    const response = statusData.response
    const resolvedWorkId = normalizeHttpIdentity(statusData.resolved_work_id) || workId
    const requestIds = Array.isArray(statusData.request_ids)
      ? statusData.request_ids
      : work?.request_ids
    const relatedRequestIds = getPendingHttpRequestIdsForWork(
      normalizedSessionId,
      workId,
      requestIds
    )
    const requestId = Array.isArray(response?.request_ids) && response.request_ids.length > 0
      ? response.request_ids[0]
      : relatedRequestIds.values().next().value || null

    if (status === 'succeeded') {
      if (response && typeof response === 'object') {
        applyTodoTransportPayload(response, normalizedSessionId)
        if (response.llm_request_metadata) {
          updateLlmRequestMetadata({
            ...response.llm_request_metadata,
            session_id: response.llm_request_metadata.session_id || normalizedSessionId
          }, () => isCurrentWritableHttpSession(normalizedSessionId))
        }
        if (response.has_background_tasks) {
          startHttpHistoryBackgroundTaskSync(normalizedSessionId)
        }
        applyNonStreamSessionEvents(response)
        if (shouldProcessCompletedWork(response)) {
          processAiResponse(response, null, requestId)
        }
      }
    } else {
      const errorMessage = statusData.error || response?.error || t('chat.send_failed')
      const resultAlreadyInHistory = hasHttpResultMessage(
        chatState.messages.value,
        statusData.result_message_id
      )
      const inserted = resultAlreadyInHistory
        ? false
        : messageProcessor.processStreamError(
            chatState.messages,
            errorMessage,
            null,
            requestId,
            resolvedWorkId,
            null,
            statusData.result_message_id
          )
      if (inserted) ElMessage.error(errorMessage)
    }

    finishHttpWorkLifecycle({
      sessionId: normalizedSessionId,
      workId,
      resolvedWorkId,
      requestIds: Array.from(relatedRequestIds)
    })
    resolvedHttpWorks.add(workId)
    resolvedHttpWorks.add(resolvedWorkId)
    return {
      active: false,
      terminal: true,
      succeeded: status === 'succeeded',
      resultMessageId: statusData.result_message_id
    }
  }

  const fetchHttpWorkStatus = async (work, sessionId) => {
    const workId = normalizeHttpIdentity(work?.work_id)
    const normalizedSessionId = normalizeHttpIdentity(sessionId)
    if (
      !workId
      || !isCurrentWritableHttpSession(normalizedSessionId)
      || fetchingHttpWorks.has(workId)
      || resolvedHttpWorks.has(workId)
    ) return null

    const stateVersion = httpPollingStateVersion
    fetchingHttpWorks.add(workId)
    try {
      const res = await chatApi.replyWorkStatus(workId)
      if (
        stateVersion !== httpPollingStateVersion
        || !isCurrentWritableHttpSession(normalizedSessionId)
        || !initialHistoryLoaded.value
      ) return null

      const statusData = res.data?.data
      if (!statusData) throw new Error('Missing reply work status')
      const result = applyHttpWorkStatus(work, statusData, normalizedSessionId)
      if (result?.active || !result?.terminal) {
        observedHttpWorkStatuses.delete(workId)
      }
      return result
    } catch {
      if (stateVersion === httpPollingStateVersion) {
        observedHttpWorkStatuses.delete(workId)
        if (isCurrentWritableHttpSession(normalizedSessionId)) {
          ElMessage.error(t('chat.result_sync_failed'))
        }
      }
      return null
    } finally {
      fetchingHttpWorks.delete(workId)
    }
  }

  const processHttpSessionSnapshot = async (sessions) => {
    if (
      !Array.isArray(sessions)
      || transport.transportMode.value !== 'http'
      || !sessionManager.currentSessionId.value
      || isCurrentSessionReadOnly.value
    ) return

    const sessionId = normalizeHttpIdentity(sessionManager.currentSessionId.value)
    const session = sessions.find(item => normalizeHttpIdentity(item?.session_id) === sessionId)
    if (!session) return

    maybeMergeHttpSessionHistory(sessionId, session.latest_message_id)

    const works = Array.isArray(session.reply_works) ? session.reply_works : []
    const activeStatuses = ['ready_for_llm', 'running', 'waiting_external_work']
    let hasActiveResult = false
    for (const work of works) {
      const workId = normalizeHttpIdentity(work?.work_id)
      const status = String(work?.status || '').toLowerCase()
      if (!workId || !status) continue

      const previousStatus = observedHttpWorkStatuses.get(workId)
      const hasPendingRequest = hasPendingHttpRequestForWork(
        sessionId,
        workId,
        work.request_ids
      )
      if (
        (initialHistoryLoaded.value || activeStatuses.includes(status))
        && !(status === 'merged' && session.is_loading === true && activeStatuses.includes(previousStatus))
      ) {
        observedHttpWorkStatuses.set(workId, status)
      }

      if (!shouldFetchHttpWorkStatus({
        status,
        previousStatus,
        hasPendingRequest,
        historyLoaded: initialHistoryLoaded.value,
        sessionLoading: session.is_loading,
        resolved: resolvedHttpWorks.has(workId),
        fetching: fetchingHttpWorks.has(workId)
      })) continue

      const result = await fetchHttpWorkStatus(work, sessionId)
      if (result?.active) hasActiveResult = true
      if (
        result?.succeeded
        && initialHistoryLoaded.value
        && normalizeHttpIdentity(sessionManager.currentSessionId.value) === sessionId
      ) {
        await mergeLatestSessionHistory(sessionId)
      }
    }

    if (!isCurrentWritableHttpSession(sessionId)) return
    const currentSnapshot = sessionManager.sessions.value.find(
      item => normalizeHttpIdentity(item?.session_id) === sessionId
    )
    if (currentSnapshot) {
      chatState.loading.value = Boolean(currentSnapshot.is_loading) || hasActiveResult
    }
  }

  const handleSessionsUpdated = sessions => {
    try {
      void processHttpSessionSnapshot(sessions).catch(err => {
        console.error('HTTP session list work processing failed:', err)
      })
    } catch (err) {
      console.error('HTTP session list update callback failed:', err)
    }
  }

  sessionManager.setSessionsUpdatedCallback(handleSessionsUpdated)
  sessionManager.setPendingSubmissionCallback(() => transport.transportMode.value === 'http' && [...pendingHttpRequests.values()].some(p => p.sessionId === normalizeHttpIdentity(sessionManager.currentSessionId.value)))

  watch(
    () => transport.transportMode.value,
    (nextMode, previousMode) => {
      if (nextMode === 'http' && previousMode !== 'http') {
        if (!sessionManager.currentSessionId.value || isCurrentSessionReadOnly.value) return
        resetHttpPollingState()
        void sessionManager.refreshSessionLoadingState().catch(err => {
          console.error('HTTP session snapshot refresh after transport mode switch failed:', err)
        })
      } else if (nextMode === 'ws') {
        resetHttpPollingState()
      }
    }
  )

  const resumeSelectedSessionStream = async (session, historyData = []) => {
    const sessionId = session?.session_id
    const latestSession = sessionManager.sessions.value.find(item => item.session_id === sessionId) || session
    const isCurrentSession = () => sessionId === sessionManager.currentSessionId.value
    if (!isCurrentSession()) return

    const mergeResumedHistory = () => {
      if (!isCurrentSession()) return
      void mergeLatestSessionHistory(sessionId).catch(err => {
        console.error('WebSocket resume history merge failed:', err)
      })
    }

    const callbacks = {
      ...createLifecycleCallbacks(isCurrentSession),
      deferLoadingUntilResumeComplete: true,
      onContextSummaryStart: (data) => {
        if (!isCurrentSession() || contextSummaryTracker.shouldIgnoreExternalSessionEvent(data, sessionId)) return
        contextSummaryTracker.startContextSummaryWork(
          contextSummaryWorkKeys.value,
          contextSummaryRequestKeys,
          data,
          data.request_id
        )
      },
      onContextSummaryEnd: (data) => {
        if (!isCurrentSession() || contextSummaryTracker.shouldIgnoreExternalSessionEvent(data, sessionId)) return
        contextSummaryTracker.endContextSummaryWork(
          contextSummaryWorkKeys.value,
          contextSummaryRequestKeys,
          data,
          data.request_id
        )
      },
      onReasoning: (text, turn, responseId, requestId, workId, eventId) => {
        if (!isCurrentSession()) return
        if (workLifecycleTracker.isWorkTerminal(workId)) return
        messageProcessor.processStreamReasoning(
          chatState.messages,
          text,
          turn,
          responseId,
          requestId,
          workId,
          eventId
        )
      },
      onContent: (text, turn, _thinkingId, finishReason, responseId, requestId, workId, eventId) => {
        if (!isCurrentSession()) return
        if (workLifecycleTracker.isWorkTerminal(workId)) return
        messageProcessor.processStreamContent(
          chatState.messages,
          text,
          turn,
          null,
          finishReason,
          responseId,
          requestId,
          workId,
          eventId
        )
      },
      onToolStart: (toolCall, _thinkingId, responseId, requestId, workId) => {
        if (!isCurrentSession()) return
        if (workLifecycleTracker.isWorkTerminal(workId)) return
        if (!currentSessionShowToolCalls.value) return
        messageProcessor.processStreamToolStart(chatState.messages, toolCall, null, responseId, requestId, workId)
      },
      onToolEnd: (toolEnd, responseId, requestId, workId) => {
        if (!isCurrentSession()) return
        if (workLifecycleTracker.isWorkTerminal(workId)) return
        if (!currentSessionShowToolCalls.value) return
        messageProcessor.processStreamToolEnd(chatState.messages, toolEnd, responseId, requestId, workId)
      },
      onComplete: (data, _thinkingId, requestId, eventType) => {
        if (!isCurrentSession()) return
        if (eventType === 'turn_end') {
          chatState.messages.value = applyResumedTurnEnd(chatState.messages.value, data, requestId)
        }
        mergeResumedHistory()
      },
      onResumeComplete: () => {
        refreshSessionLoadingState()
        mergeResumedHistory()
      },
      onError: (errorMessage, _thinkingId, requestId, errorData = {}) => {
        if (!isCurrentSession()) return
        const inserted = messageProcessor.processStreamError(
          chatState.messages,
          errorMessage,
          null,
          requestId,
          errorData.work_id,
          errorData.event_id
        )
        if (inserted) ElMessage.error(errorMessage || t('chat.stream_error'))
        mergeResumedHistory()
      },
      onProactiveReply: mergeResumedHistory,
      onProactiveReplyError: (data) => {
        if (!isCurrentSession()) return
        const errorMessage = truncateErrorMessage(data.content || data.message || 'Background proactive reply failed')
        const inserted = messageProcessor.processStreamError(
          chatState.messages,
          errorMessage,
          null,
          null,
          data.work_id,
          data.event_id
        )
        if (inserted) ElMessage.error(errorMessage)
        mergeResumedHistory()
      },
      onAuditConfirmationStatus: applyAuditConfirmationStatus,
      onAuditToolResultsUpdate: applyAuditToolResultsUpdate,
      setLoading: (value) => {
        if (!isCurrentSession()) return
        if (!value && chatState.messages.value.some(message => message.role === 'thinking')) return
        chatState.loading.value = value
      }
    }

    try {
      await resumeSessionStream({
        session,
        latestSession,
        transportMode: transport.transportMode.value,
        isCurrentSession,
        setLoading: value => { chatState.loading.value = value },
        resume: () => transport.resumeSession({
          sessionId,
          historyMessageId: Math.max(0, ...historyData.map(message => message.id)),
          callbacks
        })
      })
    } catch (err) {
      console.error('WebSocket会话恢复失败:', err)
    }
  }

  transport.setReconnectHandler(createSessionReconnectHandler({
    getCurrentSessionId: () => sessionManager.currentSessionId.value,
    getSession: sessionId => sessionManager.sessions.value.find(item => item.session_id === sessionId),
    getHistoryMessages: () => chatState.messages.value,
    resumeSession: resumeSelectedSessionStream
  }))

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
    chatState.messages.value = workLifecycleTracker.startRequestLifecycle(
      chatState.messages.value,
      { request_id: requestId }
    )
    chatState.loading.value = true
    nextTick(() => chatState.scrollToBottom())

    const requestSessionId = sessionManager.currentSessionId.value
    const newProfileOverrideId = getNewSessionProfileOverrideId(
      requestSessionId,
      newSessionProfileOverrideId.value
    )
    await performHttpSend(
      text,
      attachmentsToSent,
      userMsgId,
      requestSessionId,
      requestId,
      newProfileOverrideId,
      currentSessionShowToolCalls.value,
      currentSessionShowReasoning.value
    )
  }

  /**
   * 实际执行 HTTP 请求（支持自动二次请求）
   */
  const performHttpSend = async (text, attachmentsToSent = [], userMsgId = null, requestSessionId = null, requestId = null, profileOverrideId = null, showToolCalls = true, showReasoning = true) => {
    const isCurrentRequestSession = () => requestSessionId === sessionManager.currentSessionId.value
    try {
      const response = await transport.httpSend({
        message: text,
        sessionId: requestSessionId,
        attachments: attachmentsToSent,
        requestId,
        profileOverrideId,
        showToolCalls,
        showReasoning
      })

      // 处理后端生成的 UUID (新建会话模式)
      if (response.choices?.[0]?.finish_reason === 'new_session') {
        if (requestSessionId !== sessionManager.currentSessionId.value) return
        const newId = response.choices[0].message.content
        console.log('HTTP 模式同步新会话 ID 并触发标题生成:', newId)
        
        // 1. 设置当前会话 ID (静默选择)
        markNewSessionTodoKnownEmpty(newId)
        selectNewSession({
          session_id: newId,
          title: t('chat.default_title'),
          enable_markdown: enableMarkdownDefault.value,
          show_tool_calls: showToolCalls,
          show_reasoning: showReasoning,
          profile_override_id: profileOverrideId,
          source: 'http'
        })
        
        // 同步新建会话的 Markdown 设置
        if (enableMarkdownDefault.value) {
          chatApi.updateSessionSetting(newId, { enable_markdown: true }).catch(() => {})
        }
        
        // 2. 收到 ID 后立即调用标题生成
        sessionManager.updateSessionTitle(newId, text)
        
        // 3. 自动发起第二次真实请求
        return performHttpSend(text, attachmentsToSent, userMsgId, newId, requestId, profileOverrideId, showToolCalls, showReasoning)
      }

      if (requestSessionId !== sessionManager.currentSessionId.value) return

      if (!requestSessionId) throw new Error(t('chat.send_failed'))

      const responseSessionId = normalizeHttpIdentity(response?.session_id)
      const currentSessionId = normalizeHttpIdentity(sessionManager.currentSessionId.value)
      const workId = normalizeHttpIdentity(response?.work_id)
      if (
        !workId
        || !responseSessionId
        || responseSessionId !== normalizeHttpIdentity(requestSessionId)
        || responseSessionId !== currentSessionId
      ) throw new Error('Invalid HTTP submission acknowledgement')

      trackHttpSubmission(requestId, requestSessionId, workId)
      applyNonStreamSessionEvents(response)

      const submissionStatus = String(response?.submission_status || '').toLowerCase()
      if (submissionStatus.includes('queued')) {
        chatState.messages.value = workLifecycleTracker.markInputQueued(
          chatState.messages.value,
          { request_id: requestId, work_id: workId, session_id: requestSessionId }
        )
      } else {
        chatState.messages.value = workLifecycleTracker.markInputsDequeued(
          chatState.messages.value,
          { request_ids: [requestId], work_id: workId, session_id: requestSessionId }
        )
      }
      void sessionManager.refreshSessionLoadingState()

    } catch (err) {
      if (requestSessionId !== sessionManager.currentSessionId.value) return

      const hasBusinessRejection = Boolean(err?.response?.data?.code)
      if (!requestSessionId || hasBusinessRejection) {
        finishRequestLifecycle(requestId, isCurrentRequestSession)
        ElMessage.error(err.message || t('chat.send_failed'))
        if (!chatState.messages.value.some(message => message.role === 'thinking')) {
          chatState.loading.value = false
        }
        return
      }

      trackHttpSubmission(requestId, requestSessionId)
      const normalizedRequestId = normalizeHttpIdentity(requestId)
      chatState.messages.value = chatState.messages.value.filter(message => {
        if (message?.role !== 'thinking') return true
        const messageRequestIds = [
          message.request_id,
          ...(Array.isArray(message.request_ids) ? message.request_ids : [])
        ].map(normalizeHttpIdentity)
        return !messageRequestIds.includes(normalizedRequestId)
      })
      if (!chatState.messages.value.some(message => message.role === 'thinking')) {
        chatState.loading.value = false
      }
      ElMessage.warning(t('chat.submit_outcome_unknown'))
      void sessionManager.refreshSessionLoadingState()
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
    chatState.messages.value = workLifecycleTracker.startRequestLifecycle(
      chatState.messages.value,
      { request_id: requestId }
    )
    chatState.loading.value = true
    nextTick(() => chatState.scrollToBottom())

    let requestSessionId = sessionManager.currentSessionId.value
    const newProfileOverrideId = getNewSessionProfileOverrideId(
      requestSessionId,
      newSessionProfileOverrideId.value
    )
    const showToolCalls = currentSessionShowToolCalls.value
    const showReasoning = currentSessionShowReasoning.value
    const isCurrentRequestSession = () => requestSessionId === sessionManager.currentSessionId.value

    // 直接包装需要传递给 transport.wsSend 的 callbacks 选项
    const callbacks = {
      ...createLifecycleCallbacks(isCurrentRequestSession),
      onContextSummaryStart: (data) => {
        if (isCurrentRequestSession() && !contextSummaryTracker.shouldIgnoreExternalSessionEvent(data, sessionManager.currentSessionId.value)) {
          contextSummaryTracker.startContextSummaryWork(contextSummaryWorkKeys.value, contextSummaryRequestKeys, data, requestId)
        }
      },
      onContextSummaryEnd: (data) => {
        if (isCurrentRequestSession() && !contextSummaryTracker.shouldIgnoreExternalSessionEvent(data, sessionManager.currentSessionId.value)) {
          contextSummaryTracker.endContextSummaryWork(contextSummaryWorkKeys.value, contextSummaryRequestKeys, data, requestId)
        }
      },
      onReasoning: (text, turn, responseId, requestIdParam, workId, eventId) => {
        if (!isCurrentRequestSession()) return
        if (workLifecycleTracker.isWorkTerminal(workId)) return
        messageProcessor.processStreamReasoning(chatState.messages, text, turn, responseId, requestIdParam, workId, eventId)
      },
      onContent: (text, turn, thinkingIdParam, finishReason, responseId, requestIdParam, workId, eventId) => {
        if (!isCurrentRequestSession()) return
        if (workLifecycleTracker.isWorkTerminal(workId)) return
        messageProcessor.processStreamContent(chatState.messages, text, turn, null, finishReason, responseId, requestIdParam, workId, eventId)
      },
      onToolStart: (toolCall, thinkingIdParam, responseId, requestIdParam, workId) => {
        if (!isCurrentRequestSession()) return
        if (workLifecycleTracker.isWorkTerminal(workId)) return
        if (!currentSessionShowToolCalls.value) return
        messageProcessor.processStreamToolStart(chatState.messages, toolCall, null, responseId, requestIdParam, workId)
      },
      onToolEnd: (toolEnd, responseId, requestIdParam, workId) => {
        if (!isCurrentRequestSession()) return
        if (workLifecycleTracker.isWorkTerminal(workId)) return
        if (!currentSessionShowToolCalls.value) return
        messageProcessor.processStreamToolEnd(chatState.messages, toolEnd, responseId, requestIdParam, workId)
      },
      onError: (errorMessage, thinkingIdParam, requestIdParam, errorData = {}) => {
        contextSummaryTracker.clearContextSummaryRequest(contextSummaryWorkKeys.value, contextSummaryRequestKeys, requestIdParam || requestId)
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
        if (data.llm_request_metadata) {
          updateLlmRequestMetadata({
            ...data.llm_request_metadata,
            session_id: data.session_id || sessionManager.currentSessionId.value
          }, isCurrentRequestSession)
        }
        if (
          Array.isArray(data?.request_ids) &&
          data.request_ids.some(id => String(id) === String(requestId))
        ) {
          if (shouldApplyOwnProactiveReply(workLifecycleTracker, data, requestId)) {
            processAiResponse(data, null, requestId)
          }
          return
        }
        const workId = data.work_id
        if (
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
        const errorMessage = truncateErrorMessage(data.content || data.message || 'Background proactive reply failed')
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
        markNewSessionTodoKnownEmpty(newSessionId)
        selectNewSession({
          session_id: newSessionId,
          title: t('chat.default_title'),
          enable_markdown: enableMarkdownDefault.value,
          show_tool_calls: showToolCalls,
          show_reasoning: showReasoning,
          profile_override_id: newProfileOverrideId,
          source: 'ws'
        })
        
        // 同步新建会话的 Markdown 设置
        if (enableMarkdownDefault.value) {
          chatApi.updateSessionSetting(newSessionId, { enable_markdown: true }).catch(() => {})
        }
        
        // 2. 收到 ID 后立即调用标题生成
        sessionManager.updateSessionTitle(newSessionId, text)
      },
      onComplete: (data, thinkingIdParam, requestIdParam, eventType) => {
        if (eventType !== 'turn_end') {
          contextSummaryTracker.clearContextSummaryRequest(contextSummaryWorkKeys.value, contextSummaryRequestKeys, requestIdParam || requestId)
        }
        if (!isCurrentRequestSession()) return
        if (data.session_id && data.session_id !== requestSessionId) return

        // 每个 response_id 只保留一条正文；工具轮次的正文归入工具消息
        if (eventType === 'turn_end') {
          if (workLifecycleTracker.isWorkTerminal(getCompletedWorkId(data))) return
          const displayContent = resolveAssistantDisplayContent(data.content, data.refusal, data.finish_reason)
          const hasDisplayContent = typeof displayContent === 'string'
            ? Boolean(displayContent.trim())
            : displayContent !== undefined && displayContent !== null
          const hasTurnBody = (typeof data.content === 'string'
            ? Boolean(data.content.trim())
            : data.content !== undefined && data.content !== null) ||
            (typeof data.refusal === 'string' && Boolean(data.refusal.trim()))
          const responseFields = {
            ...(data.message_id !== null && data.message_id !== undefined && data.message_id !== '' ? { db_id: data.message_id } : {}),
            ...(typeof data.finish_reason === 'string' && data.finish_reason ? { finish_reason: data.finish_reason } : {}),
            ...(data.finish_details && typeof data.finish_details === 'object' && Object.keys(data.finish_details).length > 0 ? { finish_details: data.finish_details } : {}),
            ...(typeof data.reasoning_content === 'string' && data.reasoning_content.trim() ? { reasoning_content: data.reasoning_content } : {}),
            ...(typeof data.refusal === 'string' && data.refusal ? { refusal: data.refusal } : {}),
            ...(data.provider_metadata && typeof data.provider_metadata === 'object' && Object.keys(data.provider_metadata).length > 0 ? { provider_metadata: data.provider_metadata } : {}),
            ...(data.message_provider_metadata && typeof data.message_provider_metadata === 'object' && Object.keys(data.message_provider_metadata).length > 0 ? { message_provider_metadata: data.message_provider_metadata } : {})
          }
          if (data.response_id) {
            const matchingIndexes = chatState.messages.value
              .map((message, index) => ({ message, index }))
              .filter(item => item.message.response_id === data.response_id && item.message.role === 'assistant')
            const toolItem = matchingIndexes.find(item => isToolCall(item.message))
            const plainItems = matchingIndexes.filter(item => !isToolCall(item.message))
            const targetItem = toolItem || plainItems[0]

            if (targetItem) {
              const targetMessage = targetItem.message
              const targetContent = toolItem
                ? normalizeMessageContent(targetMessage.content)?.content
                : targetMessage.content
              const targetHasContent = typeof targetContent === 'string'
                ? Boolean(targetContent.trim())
                : targetContent !== undefined && targetContent !== null
              const shouldApplyDisplayContent = hasDisplayContent && (hasTurnBody || !targetHasContent)
              const updatedMessage = !shouldApplyDisplayContent
                ? { ...targetMessage, ...responseFields, work_id: targetMessage.work_id || data.work_id }
                : toolItem
                  ? {
                      ...targetMessage,
                      content: JSON.stringify({
                        ...normalizeMessageContent(targetMessage.content),
                        content: displayContent
                      }),
                      ...responseFields,
                      work_id: targetMessage.work_id || data.work_id
                    }
                  : { ...targetMessage, content: displayContent, ...responseFields, work_id: targetMessage.work_id || data.work_id }
              const duplicateIndexes = new Set(
                matchingIndexes
                  .filter(item => item.index !== targetItem.index)
                  .map(item => item.index)
              )
              chatState.messages.value = chatState.messages.value
                .map((message, index) => index === targetItem.index ? updatedMessage : message)
                .filter((_, index) => !duplicateIndexes.has(index))
            } else if (hasDisplayContent) {
              messageProcessor.processStreamContent(
                chatState.messages,
                displayContent,
                data.turn,
                null,
                data.finish_reason,
                data.response_id,
                requestIdParam,
                data.work_id,
                data.event_id
              )
              const createdIndex = chatState.messages.value.findLastIndex(message =>
                message.response_id === data.response_id && message.role === 'assistant' && !isToolCall(message)
              )
              if (createdIndex !== -1) {
                chatState.messages.value[createdIndex] = {
                  ...chatState.messages.value[createdIndex],
                  ...responseFields
                }
              }
            }
          } else {
            const relatedIndex = chatState.messages.value.findLastIndex(message =>
              message.role === 'assistant' &&
              !isToolCall(message) &&
              (data.work_id !== undefined && data.work_id !== null
                ? String(message.work_id) === String(data.work_id)
                : requestIdParam !== undefined && requestIdParam !== null && message.request_id === requestIdParam)
            )
            if (relatedIndex !== -1) {
              const relatedMessage = chatState.messages.value[relatedIndex]
              const relatedHasContent = typeof relatedMessage.content === 'string'
                ? Boolean(relatedMessage.content.trim())
                : relatedMessage.content !== undefined && relatedMessage.content !== null
              chatState.messages.value[relatedIndex] = {
                ...relatedMessage,
                ...(hasDisplayContent && (hasTurnBody || !relatedHasContent) ? { content: displayContent } : {}),
                ...responseFields,
                work_id: relatedMessage.work_id || data.work_id
              }
            } else if (hasDisplayContent) {
              messageProcessor.processStreamContent(
                chatState.messages,
                displayContent,
                data.turn,
                null,
                data.finish_reason,
                data.response_id,
                requestIdParam,
                data.work_id,
                data.event_id
              )
              const createdIndex = chatState.messages.value.findLastIndex(message =>
                message.role === 'assistant' &&
                !isToolCall(message) &&
                (data.work_id !== undefined && data.work_id !== null
                  ? String(message.work_id) === String(data.work_id)
                  : requestIdParam !== undefined && requestIdParam !== null && message.request_id === requestIdParam)
              )
              if (createdIndex !== -1) {
                chatState.messages.value[createdIndex] = {
                  ...chatState.messages.value[createdIndex],
                  ...responseFields
                }
              }
            }
          }
          messageProcessor.finalizeStreamReasoning(
            chatState.messages,
            data.reasoning_content,
            data.turn,
            data.response_id,
            requestIdParam,
            data.work_id
          )
          return // turn_end 时不需要执行 done 的历史比对和占位符清理
        }

        if (!shouldProcessCompletedWork(data)) return

        const completedResponse = data.response || data
        const auditConfirmation = parseAuditConfirmationResponse(completedResponse)
        if (auditConfirmation) {
          const auditRecordId = String(auditConfirmation.audit_record_id || '')
          const existingIndex = chatState.messages.value.findIndex(message => getAuditConfirmationRecordId(message) === auditRecordId)
          if (existingIndex === -1) {
            processAiResponse(completedResponse, null, requestIdParam)
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
        const finalResponse = {
          ...completedResponse,
          ...(completedResponse?.work_id == null && data.work_id != null ? { work_id: data.work_id } : {}),
          ...(completedResponse?.response_id == null && data.response_id != null ? { response_id: data.response_id } : {}),
          ...(completedResponse?.message_id == null && data.message_id != null ? { message_id: data.message_id } : {}),
          ...(completedResponse?.files == null && data.files != null ? { files: data.files } : {})
        }
        processAiResponse(finalResponse, null, requestIdParam ?? requestId)

      },
      setLoading: (val) => {
        if (!isCurrentRequestSession()) return
        if (!val && chatState.messages.value.some(message => message.role === 'thinking')) return
        chatState.loading.value = val
      }
    }
    
    const handleWsSendFailure = async () => {
      contextSummaryTracker.clearContextSummaryRequest(contextSummaryWorkKeys.value, contextSummaryRequestKeys, requestId)
      if (!isCurrentRequestSession()) return

      finishRequestLifecycle(requestId, isCurrentRequestSession)
      if (!chatState.messages.value.some(message => message.role === 'thinking')) {
        chatState.loading.value = false
      }

      const switched = await setTransportMode('http', { notifyError: false })
      if (!switched) {
        transportFallbackNotice.value = t('chat.ws_fallback_blocked')
        return
      }

      transportFallbackNotice.value = t('chat.ws_fallback_retrying')
      await httpSend(text, attachmentsToSent, userMsgId)
    }

    try {
      const sent = await transport.wsSend({
        message: text,
        sessionId: requestSessionId,
        attachments: attachmentsToSent,
        requestId,
        profileOverrideId: newProfileOverrideId,
        showToolCalls,
        showReasoning,
        callbacks
      })
      if (!sent) {
        await handleWsSendFailure()
        return
      }

      const acknowledgement = await transport.waitForSubmissionAcknowledgement(requestId)
      if (acknowledgement.status === 'unknown') {
        transportFallbackNotice.value = t('chat.ws_submission_unknown')
        void sessionManager.refreshSessionLoadingState()
      }
    } catch (e) {
      console.error('WebSocket发送失败:', e)
      await handleWsSendFailure()
    }
  }

  // ==================== 会话选择 ====================
  
  // 选择会话；session 为会话对象
  const selectSession = (session) => {
    clearTransportFallbackNotice()
    resetHttpPollingState()
    historyMergeTracker.invalidate()
    contextSummaryTracker.clearAllContextSummaryWorks(contextSummaryWorkKeys.value, contextSummaryRequestKeys)
    chatState.messages.value = workLifecycleTracker.resetWorkLifecycle(chatState.messages.value)
    initialHistoryLoaded.value = false
    sessionManager.selectSession(session, transport.disconnectWebSocket, true, false)
    const sessionTransportMode = resolveSessionTransportMode(session)
    transport.setTransportMode(sessionTransportMode, transport.disconnectWebSocket)
    sessionManager.resetPagination()
    const loadingRefreshPromise = sessionManager.refreshSessionLoadingState().catch(err => {
      console.error('Session loading state refresh before WebSocket resume failed:', err)
    })
    chatState.clearMessages()
    chatState.inputMsg.value = ''

    chatState.loading.value = getInitialResumeLoading({ session, transportMode: transport.transportMode.value })

    const historyLoadPromise = loadInitialSessionHistory(2).catch(err => {
      console.error('Session history load before WebSocket resume failed:', err)
    })
    void Promise.all([historyLoadPromise, loadingRefreshPromise])
      .then(([historyData]) => resumeSelectedSessionByTransport({
        session,
        historyData,
        transportMode: transport.transportMode.value,
        getCurrentSessionId: () => sessionManager.currentSessionId.value,
        sessions: sessionManager.sessions.value,
        processHttpSessionSnapshot,
        resumeStream: resumeSelectedSessionStream
      }))
  }

  /**
   * 新建会话
   */
  const createNewSession = () => {    
    clearTransportFallbackNotice()
    resetHttpPollingState()
    historyMergeTracker.invalidate()
    contextSummaryTracker.clearAllContextSummaryWorks(contextSummaryWorkKeys.value, contextSummaryRequestKeys)
    chatState.messages.value = workLifecycleTracker.resetWorkLifecycle(chatState.messages.value)
    initialHistoryLoaded.value = true
    transport.setTransportMode('ws', transport.disconnectWebSocket)
    sessionManager.createNewSession(transport.disconnectWebSocket)
    refreshSessionLoadingState()
    chatState.clearMessages()
    chatState.inputMsg.value = ''
    newSessionProfileOverrideId.value = null
    showToolCallsDefault.value = true
    showReasoningDefault.value = true
    // 新建会话时重置加载状态，解除模式锁定
    chatState.loading.value = false
  }

  const handleDeleteSession = async (sessionId, name, options = {}) => {
    const deleted = await sessionManager.handleDeleteSession(sessionId, name, options)
    if (shouldReturnToWelcomeAfterSessionDelete({
      deleted,
      deletedSessionId: sessionId,
      currentSessionId: sessionManager.currentSessionId.value
    })) {
      createNewSession()
    }
    return deleted
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
    const uniqueMessages = filterNewMessages(historyData)
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
    llmRequestMetadata,
    initialHistoryLoaded,
    
    // 新增附件状态导出
    attachments,
    enableMarkdownDefault,
    showToolCallsDefault,
    showReasoningDefault,
    newSessionProfileOverrideId,

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
    currentSessionShowToolCalls,
    currentSessionShowReasoning,
    currentTodoPlan,
    isCurrentSessionReadOnly,
    
    // 状态 - 通信相关
    transportMode: transport.transportMode,
    wsConnected: transport.wsConnected,
    modeSettingSubmitting,
    transportModeChangeBlocked,
    transportFallbackNotice,
    
    // 方法 - 会话
    loadSessions: sessionManager.loadSessions,
    handleDeleteSession,
    selectSession,
    createNewSession,
    reloadCurrentSessionHistory,
    
    // 方法 - 发送
    send,
    enqueueMessage,
    httpSend,
    wsSend,
    initWebSocket: transport.initWebSocket,
    disconnectWebSocket: transport.disconnectWebSocket,
    setTransportMode,
    clearTransportFallbackNotice,
    
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
