const DEFAULT_POLL_INTERVAL_MS = 1500

export const hasLoadingSessions = sessions =>
  Array.isArray(sessions) && sessions.some(session => session?.is_loading === true)

export const createSessionListLoadingPoller = ({
  refreshSessions,
  intervalMs = DEFAULT_POLL_INTERVAL_MS,
  schedule = (callback, delay) => setTimeout(callback, delay),
  cancel = timer => clearTimeout(timer)
}) => {
  if (typeof refreshSessions !== 'function') {
    throw new TypeError('refreshSessions must be a function')
  }

  let timer = null
  let refreshPromise = null
  let refreshRequested = false
  let disposed = false

  const stop = () => {
    if (timer === null) return
    cancel(timer)
    timer = null
  }

  const sync = sessions => {
    stop()
    if (disposed || !hasLoadingSessions(sessions)) return

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