import { computed, nextTick, ref, watch } from 'vue'
import { createContextSummaryTracker } from './contextSummaryTracker.js'
import { createWorkLifecycleTracker } from './workLifecycleTracker.js'
import {
  getAuditConfirmationRecordId,
  parseAuditConfirmationResponse
} from './chatMessageHelpers.js'
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
import { truncateErrorMessage } from '../../utils/errorMessage.js'
import { filterResponseHistoryToolOutput } from '../../utils/toolOutputVisibility'

export function useChatSessionEvents({
  chatState,
  sessionManager,
  currentSession,
  currentSessionShowToolCalls,
  messageProcessor,
  mergeLatestSessionHistory,
  api,
  notify,
  translate
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

  const createStreamCallbacks = ({
    isCurrentSession,
    requestId = null,
    deferLoadingUntilResumeComplete = false
  }) => {
    const resolveRequestId = (eventRequestId, data) => (
      eventRequestId ?? data?.request_id ?? requestId
    )

    const isCurrentStreamEvent = (event) => (
      isCurrentSession()
      && (!event?.session_id || event.session_id === sessionManager.currentSessionId.value)
    )

    const syncHistory = (event = null) => {
      if (!isCurrentStreamEvent(event)) return
      const sessionId = event?.session_id || sessionManager.currentSessionId.value
      if (!sessionId) return
      void mergeLatestSessionHistory(sessionId).catch(err => {
        console.error('WebSocket stream history merge failed:', err)
      })
    }

    const setLoading = (value) => {
      if (!isCurrentSession()) return
      if (!value && chatState.messages.value.some(message => message.role === 'thinking')) return
      chatState.loading.value = value
    }

    const callbacks = {
      ...createLifecycleCallbacks(isCurrentSession),
      deferLoadingUntilResumeComplete,
      onContextSummaryStart: data => {
        if (
          !isCurrentStreamEvent(data)
          || contextSummaryTracker.shouldIgnoreExternalSessionEvent(data, sessionManager.currentSessionId.value)
          || workLifecycleTracker.isWorkTerminal(data.work_id)
        ) return
        contextSummaryTracker.startContextSummaryWork(
          contextSummaryWorkKeys.value,
          contextSummaryRequestKeys,
          data,
          resolveRequestId(data.request_id, data)
        )
      },
      onContextSummaryEnd: data => {
        if (
          !isCurrentStreamEvent(data)
          || contextSummaryTracker.shouldIgnoreExternalSessionEvent(data, sessionManager.currentSessionId.value)
        ) return
        contextSummaryTracker.endContextSummaryWork(
          contextSummaryWorkKeys.value,
          contextSummaryRequestKeys,
          data,
          resolveRequestId(data.request_id, data)
        )
      },
      onReasoning: (text, turn, responseId, requestIdParam, workId, eventId) => {
        if (!isCurrentSession() || workLifecycleTracker.isWorkTerminal(workId)) return
        messageProcessor.processStreamReasoning(
          chatState.messages,
          text,
          turn,
          responseId,
          resolveRequestId(requestIdParam),
          workId,
          eventId
        )
      },
      onContent: (text, turn, _thinkingId, finishReason, responseId, requestIdParam, workId, eventId) => {
        if (!isCurrentSession() || workLifecycleTracker.isWorkTerminal(workId)) return
        messageProcessor.processStreamContent(
          chatState.messages,
          text,
          turn,
          null,
          finishReason,
          responseId,
          resolveRequestId(requestIdParam),
          workId,
          eventId
        )
      },
      onToolStart: (toolCall, _thinkingId, responseId, requestIdParam, workId) => {
        if (
          !isCurrentSession()
          || workLifecycleTracker.isWorkTerminal(workId)
          || !currentSessionShowToolCalls.value
        ) return
        messageProcessor.processStreamToolStart(
          chatState.messages,
          toolCall,
          null,
          responseId,
          resolveRequestId(requestIdParam),
          workId
        )
      },
      onToolEnd: (toolEnd, responseId, requestIdParam, workId) => {
        if (
          !isCurrentSession()
          || workLifecycleTracker.isWorkTerminal(workId)
          || !currentSessionShowToolCalls.value
        ) return
        messageProcessor.processStreamToolEnd(
          chatState.messages,
          toolEnd,
          responseId,
          resolveRequestId(requestIdParam),
          workId
        )
      },
      onComplete: (data, _thinkingId, requestIdParam, eventType) => {
        const resolvedRequestId = resolveRequestId(requestIdParam, data)
        if (eventType !== 'turn_end') {
          contextSummaryTracker.clearContextSummaryRequest(
            contextSummaryWorkKeys.value,
            contextSummaryRequestKeys,
            resolvedRequestId
          )
        }
        if (!isCurrentStreamEvent(data)) return

        if (eventType === 'turn_end') {
          if (!workLifecycleTracker.shouldProcessTurnEnd(data?.work_id)) return
          messageProcessor.processStreamTurnEnd(chatState.messages, data, resolvedRequestId)
          if (data?.work_id && data?.response_id) {
            applyLifecycleEvent(workLifecycleTracker.stopAgentLoop, data, isCurrentSession)
          }
          return
        }

        const completedResponse = data?.response || data
        const completedMetadata = data?.llm_request_metadata || completedResponse?.llm_request_metadata
        if (completedMetadata) {
          updateLlmRequestMetadata({
            ...completedMetadata,
            session_id: completedMetadata.session_id
              || data?.session_id
              || sessionManager.currentSessionId.value
          }, isCurrentSession)
        }
        if (!shouldProcessCompletedWork(data)) return

        const finalResponse = {
          ...completedResponse,
          ...(completedResponse?.work_id == null && data?.work_id != null ? { work_id: data.work_id } : {}),
          ...(completedResponse?.response_id == null && data?.response_id != null ? { response_id: data.response_id } : {}),
          ...(completedResponse?.message_id == null && data?.message_id != null ? { message_id: data.message_id } : {}),
          ...(completedResponse?.files == null && data?.files != null ? { files: data.files } : {})
        }
        const auditConfirmation = parseAuditConfirmationResponse(finalResponse)
        if (auditConfirmation) {
          const auditRecordId = String(auditConfirmation.audit_record_id || '')
          const existingIndex = chatState.messages.value.findIndex(
            message => getAuditConfirmationRecordId(message) === auditRecordId
          )
          if (existingIndex === -1) {
            processAiResponse(finalResponse, null, resolvedRequestId)
          } else {
            const existingMessage = chatState.messages.value[existingIndex]
            chatState.messages.value[existingIndex] = {
              ...existingMessage,
              type: 'audit_confirmation',
              content: JSON.stringify(auditConfirmation),
              request_id: existingMessage.request_id || resolvedRequestId
            }
          }
          if (!deferLoadingUntilResumeComplete) setLoading(false)
          return
        }

        processAiResponse(finalResponse, null, resolvedRequestId)
      },
      onError: (errorMessage, _thinkingId, requestIdParam, errorData = {}) => {
        const resolvedRequestId = resolveRequestId(requestIdParam, errorData)
        contextSummaryTracker.clearContextSummaryRequest(
          contextSummaryWorkKeys.value,
          contextSummaryRequestKeys,
          resolvedRequestId
        )
        if (!isCurrentStreamEvent(errorData)) return

        const shouldProcess = shouldProcessCompletedWork(errorData)
        if (shouldProcess) {
          const resolvedErrorMessage = errorMessage || translate('chat.stream_error')
          const inserted = messageProcessor.processStreamError(
            chatState.messages,
            resolvedErrorMessage,
            null,
            resolvedRequestId,
            errorData.work_id,
            errorData.event_id,
            errorData.message_id
          )
          if (inserted) notify.error(resolvedErrorMessage)
        }
        syncHistory(errorData)
      },
      onProactiveReply: data => {
        if (!isCurrentStreamEvent(data)) return
        if (
          data.llm_request_metadata
          && (data.llm_request_metadata.request_purpose || !data.source || data.source === 'foreground')
        ) {
          updateLlmRequestMetadata({
            ...data.llm_request_metadata,
            session_id: data.session_id || sessionManager.currentSessionId.value
          }, isCurrentSession)
        }
        syncHistory(data)
      },
      onProactiveReplyError: data => {
        if (!isCurrentStreamEvent(data)) return

        if (shouldProcessCompletedWork(data)) {
          const errorMessage = truncateErrorMessage(
            data?.content || data?.message || 'Background proactive reply failed'
          )
          const inserted = messageProcessor.processStreamError(
            chatState.messages,
            errorMessage,
            null,
            data?.request_id ?? null,
            data?.work_id,
            data?.event_id,
            data?.message_id
          )
          if (inserted) notify.error(errorMessage)
        }
        syncHistory(data)
      },
      onResumeComplete: data => {
        if (!isCurrentStreamEvent(data)) return
        refreshSessionLoadingState()
        syncHistory(data)
      },
      onAuditConfirmationStatus: applyAuditConfirmationStatus,
      onAuditToolResultsUpdate: applyAuditToolResultsUpdate,
      setLoading
    }

    return callbacks
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
    createStreamCallbacks,
    finishRequestLifecycle,
    shouldProcessCompletedWork,
    refreshSessionLoadingState,
    applyAuditConfirmationStatus,
    applyAuditToolResultsUpdate,
    applyNonStreamSessionEvents,
    updateLlmRequestMetadata
  }
}
