import {
  applyResumedTurnEnd,
  createSessionReconnectHandler,
  getHistoryMessageCursor,
  resumeSessionStream
} from './streamResume.js'
import { truncateErrorMessage } from '../../utils/errorMessage.js'

export function useChatStreamResume({
  chatState,
  sessionManager,
  transport,
  currentSessionShowToolCalls,
  isSessionScopeActive,
  createLifecycleCallbacks,
  contextSummaryTracker,
  contextSummaryWorkKeys,
  contextSummaryRequestKeys,
  workLifecycleTracker,
  messageProcessor,
  mergeLatestSessionHistory,
  refreshSessionLoadingState,
  applyAuditConfirmationStatus,
  applyAuditToolResultsUpdate,
  getCompletedWorkId,
  notify,
  translate
}) {
  const resumeSelectedSessionStream = async (session, historyData = []) => {
    const sessionId = session?.session_id
    const latestSession = sessionManager.sessions.value.find(item => item.session_id === sessionId) || session
    const isCurrentSession = () => (
      isSessionScopeActive()
      && sessionId === sessionManager.currentSessionId.value
    )
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
        if (
          !isCurrentSession()
          || contextSummaryTracker.shouldIgnoreExternalSessionEvent(data, sessionId)
          || workLifecycleTracker.isWorkTerminal(data.work_id)
        ) return
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
          if (workLifecycleTracker.isWorkTerminal(getCompletedWorkId(data))) return
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
        if (inserted) notify.error(errorMessage || translate('chat.stream_error'))
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
        if (inserted) notify.error(errorMessage)
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
          historyMessageId: getHistoryMessageCursor(historyData),
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

  return { resumeSelectedSessionStream }
}
