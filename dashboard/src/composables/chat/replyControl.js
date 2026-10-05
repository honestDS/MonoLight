import { computed, ref } from 'vue'

export function createReplyController({
  sessionManager,
  chatState,
  currentSession,
  isCurrentSessionReadOnly,
  pendingHttpRequests,
  contextSummaryWorkKeys,
  contextSummaryRequestKeys,
  workLifecycleTracker,
  contextSummaryTracker,
  stopSession,
  isSessionScopeActive,
  resetHttpPollingState,
  mergeLatestSessionHistory,
  reportError,
  translate
}) {
  const inFlightSubmissions = new Set()
  const stoppingSessionIds = ref(new Set())

  const trackSubmission = async (getSessionId, submit) => {
    const entry = { getSessionId, promise: submit() }
    inFlightSubmissions.add(entry)
    try {
      return await entry.promise
    } finally {
      inFlightSubmissions.delete(entry)
    }
  }

  const normalizeHttpIdentity = value => (
    value === undefined || value === null || value === '' ? null : String(value)
  )

  const isStopping = computed(() => {
    const sessionId = sessionManager.currentSessionId.value
    return sessionId !== undefined
      && sessionId !== null
      && sessionId !== ''
      && stoppingSessionIds.value.has(sessionId)
  })

  const isReplyRunning = computed(() => (
    !isCurrentSessionReadOnly.value
    && Boolean(chatState.loading.value || currentSession.value?.is_reply_running || isStopping.value)
  ))

  const stopReply = async () => {
    const sessionId = sessionManager.currentSessionId.value
    const hasSessionId = sessionId !== undefined && sessionId !== null && sessionId !== ''
    if (
      !hasSessionId
      || isCurrentSessionReadOnly.value
      || !isReplyRunning.value
      || stoppingSessionIds.value.has(sessionId)
    ) return false

    stoppingSessionIds.value.add(sessionId)
    try {
      const firstStopPromise = stopSession(sessionId)
      const inFlightPromises = [...inFlightSubmissions]
        .filter(entry => entry.getSessionId() === sessionId)
        .map(entry => entry.promise)
      await firstStopPromise
      if (inFlightPromises.length > 0) {
        await Promise.allSettled(inFlightPromises)
        await stopSession(sessionId)
      }

      if (
        isSessionScopeActive()
        && sessionManager.currentSessionId.value === sessionId
      ) {
        const targetSession = sessionManager.sessions.value.find(session => session.session_id === sessionId)
        const normalizedSessionId = normalizeHttpIdentity(sessionId)
        const terminalReplyWorkStatuses = new Set(['merged', 'succeeded', 'failed', 'cancelled'])

        const sources = [
          ...chatState.messages.value,
          ...(Array.isArray(targetSession?.reply_works)
            ? targetSession.reply_works.filter(work => (
                !terminalReplyWorkStatuses.has(String(work?.status || '').toLowerCase())
              ))
            : []),
          ...Array.from(pendingHttpRequests)
            .filter(([, pending]) => pending?.sessionId === normalizedSessionId)
            .map(([requestId, pending]) => ({
              work_id: pending?.workId,
              request_id: requestId
            }))
        ]
        const workIds = new Set(
          sources
            .map(source => normalizeHttpIdentity(source?.work_id))
            .filter(Boolean)
        )
        for (const key of contextSummaryWorkKeys.value) {
          if (typeof key !== 'string' || !key.startsWith('work:')) continue
          const workId = normalizeHttpIdentity(key.slice(5))
          if (workId) workIds.add(workId)
        }
        const requestIds = new Set(
          sources
            .flatMap(source => [
              source?.request_id,
              ...(Array.isArray(source?.request_ids) ? source.request_ids : [])
            ])
            .map(normalizeHttpIdentity)
            .filter(Boolean)
        )
        const requestIdList = Array.from(requestIds)

        for (const workId of workIds) {
          const event = {
            type: 'cancelled',
            session_id: sessionId,
            work_id: workId,
            request_ids: requestIdList
          }
          chatState.messages.value = workLifecycleTracker.finishWorkLifecycle(
            chatState.messages.value,
            event
          )
          contextSummaryTracker.endContextSummaryWork(
            contextSummaryWorkKeys.value,
            contextSummaryRequestKeys,
            event,
            requestIdList[0]
          )
        }

        chatState.messages.value = workLifecycleTracker.finishWorkLifecycle(
          chatState.messages.value,
          { request_ids: requestIdList }
        )

        for (const requestId of requestIdList) {
          contextSummaryTracker.clearContextSummaryRequest(
            contextSummaryWorkKeys.value,
            contextSummaryRequestKeys,
            requestId
          )
        }

        resetHttpPollingState()
        chatState.loading.value = false
        if (targetSession) targetSession.is_reply_running = false
        void mergeLatestSessionHistory(sessionId).catch(err => {
          console.error('Stop reply history merge failed:', err)
        })
      }

      if (isSessionScopeActive()) {
        void sessionManager.refreshSessionLoadingState().catch(err => {
          console.error('Session loading state refresh after stop reply failed:', err)
        })
      }
      return true
    } catch (error) {
      reportError(error?.message || translate('chat.stop_failed'))
      return false
    } finally {
      stoppingSessionIds.value.delete(sessionId)
    }
  }

  const clearSubmissions = () => {
    inFlightSubmissions.clear()
  }

  return {
    isStopping,
    isReplyRunning,
    trackSubmission,
    stopReply,
    clearSubmissions
  }
}
