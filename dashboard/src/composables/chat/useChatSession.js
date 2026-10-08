// 聊天会话管理 composable，聚合状态、会话、通信与消息处理
import { computed, nextTick, onScopeDispose, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useChatState } from './useChatState'
import { useChatDrafts } from './useChatDrafts.js'
import { useSessionManager } from './useSessionManager'
import { useChatTransport } from './useChatTransport'
import { useMessageProcessor } from './useMessageProcessor'
import { createSessionAgentSettings } from './sessionAgentSettings.js'
import { createReplyController } from './replyControl.js'
import { createHttpReplyPolling } from './httpReplyPolling.js'
import {
  activateSelectedSessionTransportMode,
  persistSessionTransportMode,
  resolveSessionTransportMode,
  resumeSelectedSessionByTransport
} from './sessionTransportMode.js'
import { createTransportNotifier } from './transportNotifications.js'
import { withSessionActivity } from './sessionActivity.js'
import { getInitialResumeLoading } from './streamResume.js'
import { useChatHistory } from './useChatHistory.js'
import { useChatSessionEvents } from './useChatSessionEvents.js'
import { useChatHttpSend } from './useChatHttpSend.js'
import { useChatWebSocketSend } from './useChatWebSocketSend.js'
import { useChatStreamResume } from './useChatStreamResume.js'
import {
  formatTimestamp,
  getMessageTimestamp,
  getToolCallArguments,
  getToolCallContent,
  getToolCallName,
  getToolCalls,
  getToolResultContent,
  getToolResultName,
  isToolCall,
  isToolResult
} from '../../utils'
import { shouldReturnToWelcomeAfterSessionDelete } from '../../utils/chatContentReveal.js'
import { chatApi } from '../../api'
import i18n from '../../i18n'

const t = (key, ...args) => i18n.global.t(key, ...args)

export function useChatSession({ currentUid = ref(null) } = {}) {
  // ==================== 基础状态与模块 ====================
  const chatState = useChatState()
  const sessionManager = useSessionManager()
  let sessionScopeActive = true

  const attachments = ref([])
  const enableMarkdownDefault = ref(false)
  const showToolCallsDefault = ref(true)
  const showReasoningDefault = ref(true)
  const newSessionProfileOverrideId = ref(null)

  const currentSession = computed(() =>
    sessionManager.sessions.value.find(
      session => session.session_id === sessionManager.currentSessionId.value
    ) || null
  )
  const {
    goalModeDefault,
    maxTurnsDefault,
    currentSessionGoalMode,
    currentSessionMaxTurns
  } = createSessionAgentSettings({
    sessionManager,
    currentSession
  })
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
  const isCurrentSessionReadOnly = computed(() => {
    const source = currentSession.value?.source
    return Boolean(source && !['http', 'ws'].includes(source))
  })
  const chatDrafts = useChatDrafts({
    inputMsg: chatState.inputMsg,
    currentUid,
    currentSessionId: sessionManager.currentSessionId,
    isCurrentSessionReadOnly
  })
  const transport = useChatTransport()
  const messageProcessor = useMessageProcessor()

  const transportNotifier = createTransportNotifier({
    translate: t,
    showMessage: options => ElMessage(options)
  })
  const modeSettingSubmitting = ref(false)

  // ==================== 历史与会话事件 ====================
  const history = useChatHistory({
    chatState,
    sessionManager,
    transport,
    isCurrentSessionReadOnly,
    currentSessionShowToolCalls,
    isSessionScopeActive: () => sessionScopeActive,
    api: chatApi
  })
  const initialHistoryLoaded = history.initialHistoryLoaded
  const events = useChatSessionEvents({
    chatState,
    sessionManager,
    currentSession,
    currentSessionShowToolCalls,
    messageProcessor,
    mergeLatestSessionHistory: history.mergeLatestSessionHistory,
    api: chatApi
  })
  const {
    currentTodoPlan,
    applyTodoTransportPayload,
    markNewSessionTodoKnownEmpty,
    contextSummaryWorkKeys,
    contextSummaryRequestKeys,
    contextSummaryTracker,
    workLifecycleTracker,
    isContextSummarizing,
    llmRequestMetadata,
    processAiResponse,
    createLifecycleCallbacks,
    finishRequestLifecycle,
    getCompletedWorkId,
    shouldProcessCompletedWork,
    refreshSessionLoadingState,
    applyAuditConfirmationStatus,
    applyAuditToolResultsUpdate,
    applyNonStreamSessionEvents,
    updateLlmRequestMetadata
  } = events

  const pendingHttpRequests = new Map()
  let httpReplyPolling
  const resetHttpPollingState = () => httpReplyPolling.resetHttpPollingState()

  const {
    isStopping,
    isReplyRunning,
    trackSubmission,
    stopReply,
    clearSubmissions
  } = createReplyController({
    sessionManager,
    chatState,
    currentSession,
    isCurrentSessionReadOnly,
    pendingHttpRequests,
    contextSummaryWorkKeys,
    contextSummaryRequestKeys,
    workLifecycleTracker,
    contextSummaryTracker,
    stopSession: sid => chatApi.stopSession(sid),
    isSessionScopeActive: () => sessionScopeActive,
    resetHttpPollingState,
    mergeLatestSessionHistory: history.mergeLatestSessionHistory,
    reportError: message => ElMessage.error(message),
    translate: t
  })

  const transportModeChangeBlocked = computed(() => (
    Boolean(chatState.loading.value || currentSession.value?.is_reply_running || isStopping.value)
  ))

  let resumeSelectedSessionStream
  const setTransportMode = async (mode, { notifyError = true } = {}) => {
    if (modeSettingSubmitting.value) return false

    const sessionId = sessionManager.currentSessionId.value
    if (!sessionId) {
      await transport.setTransportMode(mode)
      transportNotifier.close()
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
        updateSessionSetting: (targetSessionId, payload) => chatApi.updateSessionSetting(targetSessionId, payload)
      })
      if (sessionManager.currentSessionId.value === sessionId) {
        const session = sessionManager.sessions.value.find(item => item.session_id === sessionId)
        await activateSelectedSessionTransportMode({
          session,
          mode,
          historyData: chatState.messages.value,
          applyTransportMode: targetMode => transport.setTransportMode(targetMode),
          resumeStream: resumeSelectedSessionStream
        })
      }
      transportNotifier.close()
      return true
    } catch (error) {
      if (notifyError) ElMessage.error(error.message || t('chat.setting_failed'))
      return false
    } finally {
      modeSettingSubmitting.value = false
    }
  }

  // ==================== HTTP 轮询与会话回调 ====================
  httpReplyPolling = createHttpReplyPolling({
    transport,
    sessionManager,
    isCurrentSessionReadOnly,
    chatState,
    initialHistoryLoaded,
    pendingHttpRequests,
    workLifecycleTracker,
    applyTodoTransportPayload,
    updateLlmRequestMetadata,
    startHttpHistoryBackgroundTaskSync: history.startHttpHistoryBackgroundTaskSync,
    applyNonStreamSessionEvents,
    shouldProcessCompletedWork,
    processAiResponse,
    messageProcessor,
    api: chatApi,
    mergeLatestSessionHistory: history.mergeLatestSessionHistory,
    getHistoryCursor: history.getHistoryCursor,
    mergeIncrementalSessionHistory: history.syncIncrementalSessionHistory,
    reportError: message => ElMessage.error(message),
    translate: t
  })
  const { processHttpSessionSnapshot } = httpReplyPolling
  const trackHttpSubmission = (...args) => httpReplyPolling.trackHttpSubmission(...args)

  const handleSessionsUpdated = sessions => {
    try {
      void processHttpSessionSnapshot(sessions).catch(err => {
        console.error('Session list processing failed:', err)
      })
    } catch (err) {
      console.error('Session list update callback failed:', err)
    }
  }

  const handleSessionActivityUpdated = activities => {
    if (!isCurrentSessionReadOnly.value) return
    return processHttpSessionSnapshot(activities)
  }

  sessionManager.setSessionsUpdatedCallback(handleSessionsUpdated)
  sessionManager.setSessionActivityUpdatedCallback(handleSessionActivityUpdated)

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

  const selectNewSession = (session) => {
    history.invalidateHistory()
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
    chatDrafts.adoptSessionId(activeSession.session_id)
  }

  // ==================== 会话恢复 ====================
  const streamResume = useChatStreamResume({
    chatState,
    sessionManager,
    transport,
    currentSessionShowToolCalls,
    isSessionScopeActive: () => sessionScopeActive,
    createLifecycleCallbacks,
    contextSummaryTracker,
    contextSummaryWorkKeys,
    contextSummaryRequestKeys,
    workLifecycleTracker,
    messageProcessor,
    mergeLatestSessionHistory: history.mergeLatestSessionHistory,
    refreshSessionLoadingState,
    applyAuditConfirmationStatus,
    applyAuditToolResultsUpdate,
    getCompletedWorkId,
    notify: ElMessage,
    translate: t
  })
  resumeSelectedSessionStream = streamResume.resumeSelectedSessionStream

  // ==================== 发送入口 ====================
  const sendOptions = {
    chatState,
    sessionManager,
    transport,
    attachments,
    enableMarkdownDefault,
    newSessionProfileOverrideId,
    currentSessionShowToolCalls,
    currentSessionShowReasoning,
    currentSessionGoalMode,
    currentSessionMaxTurns,
    isStopping,
    rejectReadOnlySession: () => {
      if (!isCurrentSessionReadOnly.value) return false
      ElMessage.warning(t('chat.external_session_read_only'))
      return true
    },
    isSessionScopeActive: () => sessionScopeActive,
    trackSubmission,
    markNewSessionTodoKnownEmpty,
    selectNewSession,
    workLifecycleTracker,
    api: chatApi,
    notify: ElMessage,
    translate: t
  }
  const { httpSend } = useChatHttpSend({
    ...sendOptions,
    trackHttpSubmission,
    ensureIncrementalHistoryCursor: history.ensureIncrementalHistoryCursor,
    applyNonStreamSessionEvents,
    finishRequestLifecycle
  })
  const { wsSend } = useChatWebSocketSend({
    ...sendOptions,
    createLifecycleCallbacks,
    contextSummaryTracker,
    contextSummaryWorkKeys,
    contextSummaryRequestKeys,
    messageProcessor,
    processAiResponse,
    updateLlmRequestMetadata,
    mergeLatestSessionHistory: history.mergeLatestSessionHistory,
    applyAuditConfirmationStatus,
    applyAuditToolResultsUpdate,
    getCompletedWorkId,
    shouldProcessCompletedWork,
    finishRequestLifecycle,
    setTransportMode,
    transportNotifier,
    httpSend
  })

  const send = async () => {
    if (isStopping.value) return
    if (sendOptions.rejectReadOnlySession()) return
    if (transport.transportMode.value === 'ws') {
      return wsSend(chatState.inputMsg.value, attachments.value.map(a => a.path))
    }
    return httpSend(chatState.inputMsg.value, attachments.value.map(a => a.path))
  }

  const enqueueMessage = (text, queuedAttachments = []) => {
    if (isStopping.value) return
    if (sendOptions.rejectReadOnlySession()) return

    const userMsgId = Date.now() + Math.random()
    const attachmentsToSent = queuedAttachments.map(a => a.path)
    chatState.addMessage({
      id: userMsgId,
      role: 'user',
      content: text,
      attachments: attachmentsToSent,
      created_at: Date.now() / 1000
    })
    nextTick(() => chatState.scrollToBottom())

    if (transport.transportMode.value === 'ws') {
      wsSend(text, attachmentsToSent, userMsgId)
    } else {
      httpSend(text, attachmentsToSent, userMsgId)
    }
  }

  // ==================== 会话选择 ====================
  const selectSession = (session) => {
    transportNotifier.close()
    resetHttpPollingState()
    history.invalidateHistory()
    contextSummaryTracker.clearAllContextSummaryWorks(contextSummaryWorkKeys.value, contextSummaryRequestKeys)
    chatState.messages.value = workLifecycleTracker.resetWorkLifecycle(chatState.messages.value)
    initialHistoryLoaded.value = false
    sessionManager.selectSession(session, transport.disconnectWebSocket, true, false)
    const sessionTransportMode = resolveSessionTransportMode(session)
    transport.setTransportMode(sessionTransportMode)
    sessionManager.resetPagination()
    const loadingRefreshPromise = sessionManager.refreshSessionLoadingState().catch(err => {
      console.error('Session loading state refresh before WebSocket resume failed:', err)
    })
    chatState.clearMessages()
    chatDrafts.restoreSession()

    chatState.loading.value = getInitialResumeLoading({ session, transportMode: transport.transportMode.value })

    const historyLoadPromise = history.loadInitialSessionHistory(2).catch(err => {
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

  const createNewSession = () => {
    transportNotifier.close()
    resetHttpPollingState()
    history.invalidateHistory()
    contextSummaryTracker.clearAllContextSummaryWorks(contextSummaryWorkKeys.value, contextSummaryRequestKeys)
    chatState.messages.value = workLifecycleTracker.resetWorkLifecycle(chatState.messages.value)
    initialHistoryLoaded.value = true
    transport.setTransportMode('ws')
    sessionManager.createNewSession(transport.disconnectWebSocket)
    refreshSessionLoadingState()
    chatState.clearMessages()
    chatDrafts.restoreSession()
    newSessionProfileOverrideId.value = null
    showToolCallsDefault.value = true
    showReasoningDefault.value = true
    goalModeDefault.value = true
    maxTurnsDefault.value = 5
    chatState.loading.value = false
  }

  const handleDeleteSession = async (sessionId, name, options = {}) => {
    const deleted = await sessionManager.handleDeleteSession(sessionId, name, options)
    if (deleted) chatDrafts.removeDraft(sessionId)
    if (shouldReturnToWelcomeAfterSessionDelete({
      deleted,
      deletedSessionId: sessionId,
      currentSessionId: sessionManager.currentSessionId.value
    })) {
      createNewSession()
    }
    return deleted
  }

  onScopeDispose(() => {
    sessionScopeActive = false
    transportNotifier.close()
    history.disposeHistory()
    resetHttpPollingState()
    clearSubmissions()
    contextSummaryTracker.clearAllContextSummaryWorks(contextSummaryWorkKeys.value, contextSummaryRequestKeys)
    workLifecycleTracker.resetWorkLifecycle(chatState.messages.value)
    sessionManager.setSessionsUpdatedCallback(null)
    sessionManager.setSessionActivityUpdatedCallback(null)
    transport.setReconnectHandler(null)
    transport.disconnectWebSocket()
  })

  return {
    // 状态 - 消息相关
    messages: chatState.messages,
    inputMsg: chatState.inputMsg,
    loading: chatState.loading,
    messageList: chatState.messageList,
    isContextSummarizing,
    llmRequestMetadata,
    initialHistoryLoaded,

    // 状态 - 附件与默认设置
    attachments,
    enableMarkdownDefault,
    showToolCallsDefault,
    showReasoningDefault,
    goalModeDefault,
    maxTurnsDefault,
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
    currentSessionGoalMode,
    currentSessionMaxTurns,
    currentTodoPlan,
    isCurrentSessionReadOnly,
    isStopping,
    isReplyRunning,

    // 状态 - 通信相关
    transportMode: transport.transportMode,
    wsConnected: transport.wsConnected,
    modeSettingSubmitting,
    transportModeChangeBlocked,

    // 方法 - 会话
    loadSessions: sessionManager.loadSessions,
    handleDeleteSession,
    selectSession,
    createNewSession,
    reloadCurrentSessionHistory: history.reloadCurrentSessionHistory,

    // 方法 - 发送
    send,
    stopReply,
    enqueueMessage,
    httpSend,
    wsSend,
    initWebSocket: transport.initWebSocket,
    disconnectWebSocket: transport.disconnectWebSocket,
    setTransportMode,

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
    handleScroll: history.handleScroll,
    bindScrollEvent: history.bindScrollEvent,
    unbindScrollEvent: history.unbindScrollEvent
  }
}
