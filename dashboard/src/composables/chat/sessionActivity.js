export const formatSessionActivityTime = (date = new Date()) => {
  const value = date instanceof Date ? date : new Date(date)
  return [
    value.getFullYear(),
    String(value.getMonth() + 1).padStart(2, '0'),
    String(value.getDate()).padStart(2, '0')
  ].join('-') + ' ' + [
    String(value.getHours()).padStart(2, '0'),
    String(value.getMinutes()).padStart(2, '0'),
    String(value.getSeconds()).padStart(2, '0')
  ].join(':')
}

export const withSessionActivity = (session, now = new Date()) => ({
  ...session,
  last_active: session?.last_active || formatSessionActivityTime(now)
})

const sessionActivityFields = [
  'latest_message_id',
  'last_active',
  'source',
  'is_loading',
  'is_reply_running'
]

export const mergeSessionActivities = (sessions, activities) => {
  if (!Array.isArray(sessions)) return []
  if (!Array.isArray(activities) || activities.length === 0) return sessions

  const activitiesBySessionId = new Map()
  for (const activity of activities) {
    if (!activity || typeof activity !== 'object' || Array.isArray(activity)) continue

    const sessionId = activity.session_id
    if (sessionId === null || sessionId === undefined || (typeof sessionId === 'string' && sessionId.trim() === '')) continue

    const mergedActivity = activitiesBySessionId.get(sessionId) || {}
    for (const field of sessionActivityFields) {
      if (Object.prototype.hasOwnProperty.call(activity, field)) {
        mergedActivity[field] = activity[field]
      }
    }
    activitiesBySessionId.set(sessionId, mergedActivity)
  }

  if (activitiesBySessionId.size === 0) return sessions

  return sessions.map((session) => {
    if (!session || typeof session !== 'object') return session

    const activity = activitiesBySessionId.get(session.session_id)
    return activity ? { ...session, ...activity } : session
  }).sort((left, right) => {
    const leftTimestamp = Date.parse(left?.last_active)
    const rightTimestamp = Date.parse(right?.last_active)
    const leftTime = Number.isFinite(leftTimestamp) ? leftTimestamp : -Infinity
    const rightTime = Number.isFinite(rightTimestamp) ? rightTimestamp : -Infinity
    return leftTime === rightTime ? 0 : rightTime - leftTime
  })
}

const emptyCallback = () => {}
const isValidRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const isValidSessionId = value => typeof value === 'string' && value.trim() !== ''
const isValidActivity = value => isValidRecord(value) && isValidSessionId(value.session_id)

const normalizeActivities = (activities) => {
  const lightweight = []
  const bySessionId = new Map()

  for (const activity of activities) {
    if (!isValidActivity(activity)) continue

    const sessionId = activity.session_id
    const lightweightActivity = { session_id: sessionId }
    const cachedActivity = bySessionId.get(sessionId) || { session_id: sessionId }
    for (const field of sessionActivityFields) {
      if (!Object.prototype.hasOwnProperty.call(activity, field)) continue
      lightweightActivity[field] = activity[field]
      cachedActivity[field] = activity[field]
    }
    lightweight.push(lightweightActivity)
    bySessionId.set(sessionId, cachedActivity)
  }

  return { lightweight, bySessionId }
}

const validateSessions = (sessions) => {
  if (!Array.isArray(sessions) || sessions.some(session => !isValidRecord(session))) {
    throw new TypeError('fetchSessions must return an array of session objects')
  }
  return sessions
}

export const createSessionActivityController = ({
  fetchSessions,
  fetchActivity,
  getSessions,
  setSessions,
  getCurrentSessionId,
  onSessionsUpdated = emptyCallback,
  onActivityUpdated = emptyCallback,
  onError = console.error
}) => {
  let disposed = false
  let requestSequence = 0
  let fullSequence = 0
  let fullRequestSequence = 0
  let latestActivitySequence = 0
  let fullPromise = null
  let fullTrailing = false
  let activityPromise = null
  let replyObservation = null
  const activityCache = new Map()
  const unknownSessionIds = new Set()

  const notify = (callback, value) => Promise.resolve()
    .then(() => !disposed ? callback(value) : undefined)
    .catch(onError)

  const knownSessionIds = (sessions) => new Set(
    Array.isArray(sessions)
      ? sessions
        .filter(session => isValidActivity(session))
        .map(session => session.session_id)
      : []
  )

  const newerActivities = (sequence) => [...activityCache.values()]
    .filter(entry => entry.sequence > sequence)
    .map(entry => entry.activity)

  const startFullRefresh = () => {
    let pending
    pending = (async () => {
      let result = getSessions()

      while (!disposed) {
        fullTrailing = false
        const sequence = ++requestSequence
        fullRequestSequence = sequence

        try {
          const response = await fetchSessions()
          if (disposed) return getSessions()

          const sessions = validateSessions(response)
          const mergedSessions = mergeSessionActivities(sessions, newerActivities(sequence))
          if (disposed) return getSessions()

          setSessions(mergedSessions)
          fullSequence = sequence
          result = mergedSessions

          for (const [sessionId, entry] of activityCache) {
            if (entry.sequence <= sequence) activityCache.delete(sessionId)
          }
          const completeIds = knownSessionIds(sessions)
          for (const sessionId of unknownSessionIds) {
            if (completeIds.has(sessionId)) unknownSessionIds.delete(sessionId)
          }
          notify(onSessionsUpdated, mergedSessions)
        } catch (error) {
          if (disposed) return getSessions()
          unknownSessionIds.clear()
          fullTrailing = false
          throw error
        } finally {
          fullRequestSequence = 0
        }

        if (!fullTrailing) return result
      }

      return result
    })()

    fullPromise = pending
    pending.then(
      () => {
        if (fullPromise === pending) fullPromise = null
      },
      () => {
        if (fullPromise === pending) fullPromise = null
      }
    )
    return pending
  }

  const refreshSessions = ({ coalesce = false } = {}) => {
    if (disposed) return Promise.resolve(getSessions())
    if (fullPromise && fullRequestSequence > 0) {
      if (!coalesce || fullRequestSequence < latestActivitySequence) fullTrailing = true
      return fullPromise
    }
    return startFullRefresh()
  }

  const refreshActivity = () => {
    if (disposed) return Promise.resolve([])
    if (activityPromise) return activityPromise

    const sequence = ++requestSequence
    const pending = (async () => {
      const response = await fetchActivity()
      if (!Array.isArray(response)) throw new TypeError('fetchActivity must return an array')
      if (disposed) return []

      latestActivitySequence = sequence
      if (sequence <= fullSequence) return []

      const { lightweight, bySessionId } = normalizeActivities(response)
      const sessions = getSessions()
      const completeIds = knownSessionIds(sessions)
      const missing = [...completeIds].some(sessionId => !bySessionId.has(sessionId))

      for (const sessionId of activityCache.keys()) {
        if (!bySessionId.has(sessionId)) activityCache.delete(sessionId)
      }
      for (const [sessionId, activity] of bySessionId) {
        activityCache.set(sessionId, { sequence, activity })
      }

      if (Array.isArray(sessions)) setSessions(mergeSessionActivities(sessions, lightweight))

      for (const sessionId of unknownSessionIds) {
        if (!bySessionId.has(sessionId)) unknownSessionIds.delete(sessionId)
      }

      let discovered = false
      for (const sessionId of bySessionId.keys()) {
        if (completeIds.has(sessionId)) {
          unknownSessionIds.delete(sessionId)
        } else if (!unknownSessionIds.has(sessionId)) {
          unknownSessionIds.add(sessionId)
          discovered = true
        }
      }

      const sessionId = getCurrentSessionId()
      const selectedActivity = bySessionId.get(sessionId)
      const selectedSession = Array.isArray(sessions)
        ? sessions.find(session => isValidRecord(session) && session.session_id === sessionId)
        : null
      const source = selectedActivity && Object.prototype.hasOwnProperty.call(selectedActivity, 'source')
        ? selectedActivity.source
        : selectedSession?.source
      const isWebSession = source === 'http' || source === 'ws'
      if (!isWebSession || !replyObservation
        || replyObservation.sessionId !== sessionId
        || replyObservation.source !== source) {
        replyObservation = null
      }

      let observationPrevious = null
      let observationNext = null
      let observationRefresh = false
      if (isWebSession && selectedActivity && typeof selectedActivity.is_reply_running === 'boolean') {
        const value = selectedActivity.is_reply_running
        if (!replyObservation || replyObservation.value !== value) {
          observationPrevious = replyObservation
          observationNext = { sessionId, source, value }
          replyObservation = observationNext
          observationRefresh = observationPrevious ? true : value === true
        }
      }

      notify(onActivityUpdated, lightweight)
      if (discovered || missing || observationRefresh) {
        const refreshPromise = refreshSessions({ coalesce: true })
        refreshPromise.catch(error => {
          if (disposed) return
          if (observationNext && replyObservation === observationNext) {
            replyObservation = observationPrevious
          }
          onError(error)
        })
      }

      return lightweight
    })()

    activityPromise = pending
    pending.then(
      () => {
        if (activityPromise === pending) activityPromise = null
      },
      () => {
        if (activityPromise === pending) activityPromise = null
      }
    )
    return pending
  }

  const resetReplyObservation = () => {
    replyObservation = null
  }

  const dispose = () => {
    if (disposed) return
    disposed = true
    fullTrailing = false
    activityCache.clear()
    unknownSessionIds.clear()
    replyObservation = null
  }

  return { refreshSessions, refreshActivity, resetReplyObservation, dispose }
}
