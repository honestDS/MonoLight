import { nextTick } from 'vue'
import { getNewSessionProfileOverrideId } from '../../utils/profileOptions.js'
import { truncateErrorMessage } from '../../utils/errorMessage.js'
import { shouldApplyOwnProactiveReply } from './workLifecycleTracker.js'
import {
  getAuditConfirmationRecordId,
  parseAuditConfirmationResponse
} from './chatMessageHelpers.js'

export function useChatWebSocketSend({
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
  rejectReadOnlySession,
  isSessionScopeActive,
  trackSubmission,
  markNewSessionTodoKnownEmpty,
  selectNewSession,
  createLifecycleCallbacks,
  contextSummaryTracker,
  contextSummaryWorkKeys,
  contextSummaryRequestKeys,
  workLifecycleTracker,
  messageProcessor,
  processAiResponse,
  updateLlmRequestMetadata,
  mergeLatestSessionHistory,
  applyAuditConfirmationStatus,
  applyAuditToolResultsUpdate,
  getCompletedWorkId,
  shouldProcessCompletedWork,
  finishRequestLifecycle,
  setTransportMode,
  transportNotifier,
  httpSend,
  api,
  notify,
  translate
}) {
  const wsSend = async (textParam = null, attachmentsParam = null, existingMsgId = null) => {
    if (isStopping.value) return
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
    const goalMode = currentSessionGoalMode.value
    const maxTurns = currentSessionMaxTurns.value
    const isCurrentRequestSession = () => (
      isSessionScopeActive()
      && requestSessionId === sessionManager.currentSessionId.value
    )

    // 直接包装需要传递给 transport.wsSend 的 callbacks 选项
    const callbacks = {
      ...createLifecycleCallbacks(isCurrentRequestSession),
      onContextSummaryStart: (data) => {
        if (
          isCurrentRequestSession()
          && !contextSummaryTracker.shouldIgnoreExternalSessionEvent(data, sessionManager.currentSessionId.value)
          && !workLifecycleTracker.isWorkTerminal(data.work_id)
        ) {
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
          notify.error(errorMessage || translate('chat.stream_error'))
        }
      },
      onProactiveReply: (data) => {
        if (data.session_id && data.session_id !== sessionManager.currentSessionId.value) return
        if (
          data.llm_request_metadata
          && (data.llm_request_metadata.request_purpose || !data.source || data.source === 'foreground')
        ) {
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
          notify.error(errorMessage)
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
          title: translate('chat.default_title'),
          enable_markdown: enableMarkdownDefault.value,
          show_tool_calls: showToolCalls,
          show_reasoning: showReasoning,
          goal_mode: goalMode,
          max_turns: maxTurns,
          profile_override_id: newProfileOverrideId,
          source: 'ws'
        })
        
        // 同步新建会话的 Markdown 设置
        if (enableMarkdownDefault.value) {
          api.updateSessionSetting(newSessionId, { enable_markdown: true }).catch(() => {})
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

        if (eventType === 'turn_end') {
          if (workLifecycleTracker.isWorkTerminal(getCompletedWorkId(data))) return
          messageProcessor.processStreamTurnEnd(chatState.messages, data, requestIdParam ?? requestId)
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
        transportNotifier.show('fallback_blocked')
        return
      }

      transportNotifier.show('fallback_retrying')
      await httpSend(text, attachmentsToSent, userMsgId)
    }

    return trackSubmission(
      () => requestSessionId,
      async () => {
        try {
          const sent = await transport.wsSend({
            message: text,
            sessionId: requestSessionId,
            attachments: attachmentsToSent,
            requestId,
            profileOverrideId: newProfileOverrideId,
            showToolCalls,
            showReasoning,
            goalMode,
            maxTurns,
            callbacks
          })
          if (!sent) {
            await handleWsSendFailure()
            return
          }

          const acknowledgement = await transport.waitForSubmissionAcknowledgement(requestId)
          if (acknowledgement.status === 'unknown') {
            if (isCurrentRequestSession()) {
              transportNotifier.show('submission_unknown')
            }
            void sessionManager.refreshSessionLoadingState()
          }
        } catch (e) {
          console.error('WebSocket发送失败:', e)
          await handleWsSendFailure()
        }
      }
    )
  }

  return { wsSend }
}
