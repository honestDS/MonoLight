const DEFAULT_POLL_INTERVAL_MS = 1500

export const hasLoadingSessions = sessions =>
  Array.isArray(sessions) && sessions.some(session => session?.is_loading === true)

export const hasHttpResultMessage = (messages, resultMessageId) => {
  const normalizedResultMessageId = Number(resultMessageId)
  if (!Number.isSafeInteger(normalizedResultMessageId) || normalizedResultMessageId <= 0) return false

  return Array.isArray(messages) && messages.some(message => {
    const dbId = Number(message?.db_id)
    return Number.isSafeInteger(dbId) && dbId === normalizedResultMessageId
  })
}

export const shouldFetchHttpWorkStatus = ({
  status,
  previousStatus,
  hasPendingRequest,
  historyLoaded,
  sessionLoading,
  resolved,
  fetching
}) => {
  const terminalStatuses = ['merged', 'succeeded', 'failed', 'cancelled']
  const activeStatuses = ['ready_for_llm', 'running', 'waiting_external_work']

  return terminalStatuses.includes(status)
    && historyLoaded
    && !(status === 'merged' && sessionLoading === true)
    && !resolved
    && !fetching
    && (hasPendingRequest || activeStatuses.includes(previousStatus))
}

export const createSessionListLoadingPoller = ({
  refreshSessions,
  intervalMs = DEFAULT_POLL_INTERVAL_MS,
  schedule = (callback, delay) => setTimeout(callback, delay),
  cancel = timer => clearTimeout(timer),
  hasPendingSubmissions = () => false,
  maxPendingIdleRefreshes = 8
}) => {
  if (typeof refreshSessions !== 'function') {
    throw new TypeError('refreshSessions must be a function')
  }

  let timer = null
  let refreshPromise = null
  let refreshRequested = false
  let disposed = false
  let pendingIdleRefreshes = 0

  const stop = () => {
    if (timer === null) return
    cancel(timer)
    timer = null
  }

  const sync = sessions => {
    stop()
    if (disposed) return

    if (!hasLoadingSessions(sessions)) {
      if (!hasPendingSubmissions()) {
        pendingIdleRefreshes = 0
        return
      }
      if (pendingIdleRefreshes >= maxPendingIdleRefreshes) return
      pendingIdleRefreshes += 1
    }

    timer = schedule(async () => {
      timer = null
      await refreshNow()
    }, intervalMs)
  }

  const refreshNow = () => {
    stop()
    if (disposed) return Promise.resolve([])

    refreshRequested = true
    if (refreshPromise) return refreshPromise

    const pending = (async () => {
      let sessions = []
      while (refreshRequested && !disposed) {
        refreshRequested = false
        sessions = await refreshSessions()
      }
      if (!disposed) sync(sessions)
      return sessions
    })()

    refreshPromise = pending
    return pending.finally(() => {
      if (refreshPromise === pending) refreshPromise = null
    })
  }

  const dispose = () => {
    disposed = true
    refreshRequested = false
    stop()
  }

  return {
    refreshNow,
    sync,
    stop,
    dispose,
    isPolling: () => timer !== null
  }
}
