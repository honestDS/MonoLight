export const createHttpHistorySyncController = ({
  getSessionId,
  canSync,
  isLoading,
  onTrackingStarted = () => {},
  fetchPendingActivity,
  mergeLatestHistory,
  intervalMs = 2000,
  schedule = setTimeout,
  cancel = clearTimeout,
  onError = console.error
}) => {
  let timer = null
  let syncing = false
  let trackedSessionId = null
  let version = 0

  const shouldContinue = () => {
    const sessionId = getSessionId()
    return Boolean(sessionId)
      && canSync()
      && trackedSessionId === sessionId
  }

  const stop = () => {
    version += 1
    if (timer !== null) {
      cancel(timer)
      timer = null
    }
    syncing = false
    trackedSessionId = null
  }

  const sync = async () => {
    const sessionId = getSessionId()
    if (
      !canSync()
      || !sessionId
      || trackedSessionId !== sessionId
      || syncing
    ) return

    if (isLoading()) return

    const syncVersion = version
    syncing = true
    try {
      const hasPendingActivity = await fetchPendingActivity(sessionId)
      if (
        syncVersion !== version
        || !canSync()
        || sessionId !== getSessionId()
        || trackedSessionId !== sessionId
      ) return

      if (hasPendingActivity) return

      const mergeResult = await mergeLatestHistory(sessionId)
      if (
        syncVersion === version
        && canSync()
        && sessionId === getSessionId()
        && trackedSessionId === sessionId
        && mergeResult?.hasMore !== true
      ) {
        trackedSessionId = null
      }
    } catch (error) {
      onError(error)
    } finally {
      if (syncVersion === version) {
        syncing = false
      }
    }
  }

  const scheduleNext = () => {
    if (timer !== null) {
      cancel(timer)
      timer = null
    }

    if (!shouldContinue()) return
    const syncVersion = version

    timer = schedule(async () => {
      if (syncVersion !== version) return
      timer = null
      await sync()
      if (syncVersion === version) {
        scheduleNext()
      }
    }, intervalMs)
  }

  const start = sessionId => {
    if (!canSync() || !sessionId || sessionId !== getSessionId()) return
    trackedSessionId = sessionId
    onTrackingStarted(sessionId)
    scheduleNext()
  }

  const handleSessionChanged = async () => {
    stop()
    const sessionId = getSessionId()
    if (!canSync() || !sessionId) return

    trackedSessionId = sessionId
    onTrackingStarted(sessionId)
    const syncVersion = version
    await sync()
    if (syncVersion === version && shouldContinue()) {
      scheduleNext()
    }
  }

  return {
    stop,
    start,
    sync,
    handleSessionChanged,
    isTracking: sessionId => trackedSessionId === sessionId
  }
}
