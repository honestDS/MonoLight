import { computed, nextTick, ref, watch } from 'vue'
import { createContextSummaryTracker } from './contextSummaryTracker.js'
import { createWorkLifecycleTracker } from './workLifecycleTracker.js'
import {
  applyAuditConfirmationStatusToMessages,
  applyAuditToolResultsUpdateToMessages
} from './auditConfirmationState.js'
import {
  isMainDialogueRequestMetadata,
  mergeLlmRequestMetadata,
  normalizeLlmRequestMetadata,
  shouldReplaceLlmRequestMetadata
} from './llmRequestMetadata.js'
import {
  mergeTodoPlan,
  normalizeTodoPlan,
  readTodoPlanFromTransport
} from '../../utils/todoPresentation.js'
import { filterResponseHistoryToolOutput } from '../../utils/toolOutputVisibility'

export function useChatSessionEvents({
  chatState,
  sessionManager,
  currentSession,
  currentSessionShowToolCalls,
  messageProcessor,
  mergeLatestSessionHistory,
  api
}) {
  const currentTodoPlan = ref(null)
  const skipTodoInitialLoadSessionIds = new Set()
  let todoLoadVersion = 0

  const contextSummaryWorkKeys = ref(new Set())
  const contextSummaryRequestKeys = new Map()
  const contextSummaryTracker = createContextSummaryTracker()
  const llmRequestMetadataBySession = ref(new Map())
  const workLifecycleTracker = createWorkLifecycleTracker()

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
        const response = await api.sessionTodo(sessionId)
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

  const llmRequestMetadata = computed(() => {
    const sessionId = sessionManager.currentSessionId.value
    if (!sessionId) return null
    return llmRequestMetadataBySession.value.get(sessionId)
      || normalizeLlmRequestMetadata(currentSession.value?.llm_request_metadata)
  })

  const processAiResponse = (response, thinkingId = null, requestId = null) => {
    messageProcessor.processAiResponse(
      chatState.messages,
      filterResponseHistoryToolOutput(response, currentSessionShowToolCalls.value),
      thinkingId,
      requestId
    )
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
    if (!isMainDialogueRequestMetadata(metadata)) return

    const sessionIndex = sessionManager.sessions.value.findIndex(session => session.session_id === sessionId)
    const currentMetadata = llmRequestMetadataBySession.value.get(sessionId)
      || normalizeLlmRequestMetadata(sessionManager.sessions.value[sessionIndex]?.llm_request_metadata)
    if (!shouldReplaceLlmRequestMetadata(currentMetadata, metadata)) return

    const nextMetadata = mergeLlmRequestMetadata(currentMetadata, metadata)

    const nextMetadataBySession = new Map(llmRequestMetadataBySession.value)
    nextMetadataBySession.set(sessionId, nextMetadata)
    llmRequestMetadataBySession.value = nextMetadataBySession

    if (sessionIndex !== -1) {
      sessionManager.sessions.value[sessionIndex] = {
        ...sessionManager.sessions.value[sessionIndex],
        llm_request_metadata: nextMetadata
      }
    }
  }

  const refreshSessionLoadingState = () => {
    void sessionManager.refreshSessionLoadingState()
  }

  const createLifecycleCallbacks = isCurrentRequestSession => ({
    onInputAccepted: () => {
      refreshSessionLoadingState()
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
      if (
        isCurrentRequestSession()
        && (!event?.session_id || event.session_id === sessionManager.currentSessionId.value)
      ) {
        contextSummaryTracker.endContextSummaryWork(
          contextSummaryWorkKeys.value,
          contextSummaryRequestKeys,
          event,
          event.request_id
        )
        for (const requestId of [
          event.request_id,
          ...(Array.isArray(event.request_ids) ? event.request_ids : [])
        ]) {
          if (requestId === undefined || requestId === null || requestId === '') continue
          contextSummaryTracker.clearContextSummaryRequest(
            contextSummaryWorkKeys.value,
            contextSummaryRequestKeys,
            requestId
          )
        }
      }
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

  return {
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
  }
}
