import { nextTick } from 'vue'
import { getNewSessionProfileOverrideId } from '../../utils/profileOptions.js'
import { normalizeHttpIdentity } from './httpReplyPolling.js'

export function useChatHttpSend({
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
  trackHttpSubmission,
  ensureIncrementalHistoryCursor,
  markNewSessionTodoKnownEmpty,
  selectNewSession,
  applyNonStreamSessionEvents,
  finishRequestLifecycle,
  workLifecycleTracker,
  api,
  notify,
  translate
}) {
  const t = translate

  /**
   * HTTP 方式发送消息
   */
  const httpSend = async (textParam = null, attachmentsParam = null, existingMsgId = null) => {
    if (isStopping.value) return
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
      currentSessionShowReasoning.value,
      currentSessionGoalMode.value,
      currentSessionMaxTurns.value
    )
  }

  /**
   * 实际执行 HTTP 请求（支持自动二次请求）
   */
  const performHttpSend = async (text, attachmentsToSent = [], userMsgId = null, requestSessionId = null, requestId = null, profileOverrideId = null, showToolCalls = true, showReasoning = true, goalMode = true, maxTurns = 5) => {
    if (requestSessionId) {
      ensureIncrementalHistoryCursor(requestSessionId)
    }
    const isCurrentRequestSession = () => (
      isSessionScopeActive()
      && requestSessionId === sessionManager.currentSessionId.value
    )
    try {
      const response = await trackSubmission(
        () => requestSessionId,
        () => transport.httpSend({
          message: text,
          sessionId: requestSessionId,
          attachments: attachmentsToSent,
          requestId,
          profileOverrideId,
          showToolCalls,
          showReasoning,
          goalMode,
          maxTurns
        })
      )

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
          goal_mode: goalMode,
          max_turns: maxTurns,
          profile_override_id: profileOverrideId,
          source: 'http'
        })

        // 同步新建会话的 Markdown 设置
        if (enableMarkdownDefault.value) {
          api.updateSessionSetting(newId, { enable_markdown: true }).catch(() => {})
        }

        // 2. 收到 ID 后立即调用标题生成
        sessionManager.updateSessionTitle(newId, text)

        // 3. 自动发起第二次真实请求
        return performHttpSend(text, attachmentsToSent, userMsgId, newId, requestId, profileOverrideId, showToolCalls, showReasoning, goalMode, maxTurns)
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
        notify.error(err.message || t('chat.send_failed'))
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
      notify.warning(t('chat.submit_outcome_unknown'))
      void sessionManager.refreshSessionLoadingState()
    }
  }

  return { httpSend }
}
