import { hasHttpResultMessage, shouldFetchHttpWorkStatus } from './sessionListLoading.js'
import { getLatestPersistedMessageId } from './historyIncrementalSync.js'

export const normalizeHttpIdentity = value => (
  value === undefined || value === null || value === '' ? null : String(value)
)

export function createHttpReplyPolling({
  transport,
  sessionManager,
  isCurrentSessionReadOnly,
  chatState,
  initialHistoryLoaded,
  pendingHttpRequests,
  workLifecycleTracker,
  applyTodoTransportPayload,
  updateLlmRequestMetadata,
  startHttpHistoryBackgroundTaskSync,
  applyNonStreamSessionEvents,
  shouldProcessCompletedWork,
  processAiResponse,
  messageProcessor,
  api,
  mergeLatestSessionHistory,
  getHistoryCursor = () => getLatestPersistedMessageId(chatState.messages.value),
  mergeIncrementalSessionHistory,
  reportError,
  translate
}) {
  const t = translate
  const observedHttpWorkStatuses = new Map()
  const observedHttpLatestMessageIds = new Map()
  const fetchingHttpWorks = new Set()
  const resolvedHttpWorks = new Set()
  let httpPollingStateVersion = 0
  let externalHistoryState = null

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
    externalHistoryState = null
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

  const maybeMergeExternalSessionHistory = async (rawSessionId, session) => {
    const normalizedSessionId = normalizeHttpIdentity(rawSessionId)
    if (!normalizedSessionId) return

    if (externalHistoryState?.sessionId !== normalizedSessionId) {
      externalHistoryState = {
        sessionId: normalizedSessionId,
        processedLatestMessageId: 0,
        fetching: false
      }
    }

    const syncState = externalHistoryState
    if (!initialHistoryLoaded.value || syncState.fetching) return

    const rawLatestMessageId = session?.latest_message_id
    if (typeof rawLatestMessageId !== 'number' && typeof rawLatestMessageId !== 'string') return

    const latestMessageId = Number(rawLatestMessageId)
    if (!Number.isSafeInteger(latestMessageId) || latestMessageId <= 0) return

    if (latestMessageId <= Math.max(
      getHistoryCursor(rawSessionId),
      syncState.processedLatestMessageId
    )) return

    const stateVersion = httpPollingStateVersion
    syncState.fetching = true
    const isCurrentSync = () => (
      externalHistoryState === syncState
      && stateVersion === httpPollingStateVersion
      && normalizeHttpIdentity(sessionManager.currentSessionId.value) === normalizedSessionId
      && isCurrentSessionReadOnly.value
      && initialHistoryLoaded.value
    )

    try {
      const result = await mergeIncrementalSessionHistory(rawSessionId, isCurrentSync)
      if (
        result?.hasMore === false
        && result?.cancelled !== true
        && isCurrentSync()
      ) {
        syncState.processedLatestMessageId = latestMessageId
      }
      return result
    } catch (error) {
      if (isCurrentSync()) throw error
    } finally {
      syncState.fetching = false
    }
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
    } else if (status === 'failed') {
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
      if (inserted) reportError(errorMessage)
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
      const res = await api.replyWorkStatus(workId)
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
          reportError(t('chat.result_sync_failed'))
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
      || !sessionManager.currentSessionId.value
    ) return

    const stateVersion = httpPollingStateVersion
    const rawSessionId = sessionManager.currentSessionId.value
    const sessionId = normalizeHttpIdentity(rawSessionId)
    const session = sessions.find(item => normalizeHttpIdentity(item?.session_id) === sessionId)
    if (!session) return

    if (isCurrentSessionReadOnly.value) {
      await maybeMergeExternalSessionHistory(rawSessionId, session)
      return
    }

    if (transport.transportMode.value !== 'http') return

    maybeMergeHttpSessionHistory(sessionId, session.latest_message_id)

    const works = Array.isArray(session.reply_works) ? session.reply_works : []
    const activeStatuses = ['ready_for_llm', 'running', 'waiting_external_work']
    let hasActiveResult = false
    for (const work of works) {
      if (stateVersion !== httpPollingStateVersion) return

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
        && !(status === 'merged' && session.is_reply_running === true && activeStatuses.includes(previousStatus))
      ) {
        observedHttpWorkStatuses.set(workId, status)
      }

      if (!shouldFetchHttpWorkStatus({
        status,
        previousStatus,
        hasPendingRequest,
        historyLoaded: initialHistoryLoaded.value,
        sessionLoading: session.is_reply_running,
        resolved: resolvedHttpWorks.has(workId),
        fetching: fetchingHttpWorks.has(workId)
      })) continue

      const result = await fetchHttpWorkStatus(work, sessionId)
      if (
        stateVersion !== httpPollingStateVersion
        || !isCurrentWritableHttpSession(sessionId)
      ) return

      if (result?.active) hasActiveResult = true
      if (
        result?.succeeded
        && initialHistoryLoaded.value
        && normalizeHttpIdentity(sessionManager.currentSessionId.value) === sessionId
      ) {
        await mergeLatestSessionHistory(sessionId)
      }
    }

    if (
      stateVersion !== httpPollingStateVersion
      || !isCurrentWritableHttpSession(sessionId)
    ) return
    const currentSnapshot = sessionManager.sessions.value.find(
      item => normalizeHttpIdentity(item?.session_id) === sessionId
    )
    if (currentSnapshot) {
      chatState.loading.value = Boolean(currentSnapshot.is_reply_running) || hasActiveResult
    }
  }

  return {
    trackHttpSubmission,
    resetHttpPollingState,
    applyHttpWorkStatus,
    processHttpSessionSnapshot
  }
}
