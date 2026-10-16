import { nextTick } from 'vue'
import { getNewSessionProfileOverrideId } from '../../utils/profileOptions.js'
import { getHistoryMessageCursor } from './streamResume.js'

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
  createStreamCallbacks,
  contextSummaryTracker,
  contextSummaryWorkKeys,
  contextSummaryRequestKeys,
  workLifecycleTracker,
  finishRequestLifecycle,
  setTransportMode,
  transportNotifier,
  httpSend,
  api,
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

    const historyMessageId = getHistoryMessageCursor(
      chatState.messages.value.filter(message => {
        const dbId = message?.db_id
        return dbId !== null && dbId !== undefined && dbId !== ''
      })
    )

    const callbacks = {
      ...createStreamCallbacks({
        isCurrentSession: isCurrentRequestSession,
        requestId
      }),
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
            historyMessageId,
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
