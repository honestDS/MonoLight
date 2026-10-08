import { createSessionListPoller } from './sessionListLoading.js'

const noop = () => {}
const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const isPositive = value => Number.isSafeInteger(value) && value > 0
const isCursor = value => Number.isSafeInteger(value) && value >= 0
const isSessionId = value => typeof value === 'string' && value.trim() !== ''
const isTerminal = value => value === 'succeeded' || value === 'failed'
const closeHandle = (handle, reportError) => {
  if (handle === null || handle === undefined) return
  try {
    if (typeof handle === 'function') handle()
    else if (typeof handle.close === 'function') handle.close()
  } catch (error) { reportError(error) }
}
export const getSessionReadCursor = (options = {}) => {
  const { messages, historyLoaded, visible, focused, atBottom } = options || {}
  if (historyLoaded !== true || visible !== true || focused !== true || atBottom !== true
    || !Array.isArray(messages)) return 0
  let cursor = 0
  for (const message of messages) {
    if (!isRecord(message) || message.role === 'thinking') continue
    if (isPositive(message.db_id)) cursor = Math.max(cursor, message.db_id)
  }
  return cursor
}
export const createSessionTaskController = ({
  fetchActivity,
  markRead,
  onTasksUpdated = noop,
  onActivityUpdated = noop,
  onNotify = noop,
  onError = console.error,
  intervalMs = 1500,
  schedule,
  cancel,
  nextSequence
} = {}) => {
  if (typeof fetchActivity !== 'function') throw new TypeError('fetchActivity must be a function')
  if (typeof markRead !== 'function') throw new TypeError('markRead must be a function')
  let activities = []
  let identity = null
  let generation = 0
  let poller = null
  let disposed = false
  let localSequence = 0
  let latestSequence = null
  const confirmedRead = new Map()
  const readStates = new Map()
  const notified = new Map()
  const notifications = new Map()
  const reportError = error => { try { if (typeof onError === 'function') onError(error) } catch {} }
  const call = (callback, value) => { try { callback(value) } catch (error) { reportError(error) } }
  const isCurrent = requestGeneration => !disposed && identity !== null
    && requestGeneration === generation
  const confirmed = sessionId => confirmedRead.get(sessionId) || 0
  const visible = list => list.filter(activity => activity.is_owned === true
    && (activity.is_running || activity.has_unread_result))
  const canReadSession = sessionId => {
    const activity = activities.find(item => item.session_id === sessionId)
    return !activity || activity.is_owned === true
  }
  const takeSequence = typeof nextSequence === 'function'
    ? nextSequence
    : () => { localSequence += 1; return localSequence }
  const closeNotification = sessionId => {
    const entry = notifications.get(sessionId)
    if (entry) { notifications.delete(sessionId); closeHandle(entry.handle, reportError) }
  }
  const closeAllNotifications = () => {
    for (const sessionId of notifications.keys()) closeNotification(sessionId)
  }
  const permanentReadError = error => {
    const status = error?.response?.data?.code
      ?? error?.response?.status ?? error?.status ?? error?.statusCode
    return status === 400 || status === 403 || status === 404
  }
  const publish = (nextActivities, requestGeneration) => {
    if (!isCurrent(requestGeneration)) return visible(activities)
    const serverRead = new Map()
    const snapshot = nextActivities.map(activity => ({ ...activity }))
    for (const activity of snapshot) {
      if (activity.is_owned !== true) continue
      const cursor = serverRead.get(activity.session_id) || 0
      serverRead.set(activity.session_id, Math.max(cursor, activity.last_read_message_id || 0))
    }
    for (const [sessionId, cursor] of serverRead) {
      if (cursor > confirmed(sessionId)) confirmedRead.set(sessionId, cursor)
    }
    activities = snapshot.map(activity => {
      if (activity.is_owned !== true) return activity
      const serverCursor = serverRead.get(activity.session_id) || 0
      const readCursor = Math.max(confirmed(activity.session_id), serverCursor)
      const nextActivity = {
        ...activity,
        has_unread_result: isPositive(activity.completed_message_id)
          && activity.completed_message_id > readCursor
      }
      if (readCursor > 0 || activity.last_read_message_id !== null) {
        nextActivity.last_read_message_id = readCursor
      }
      return nextActivity
    })
    for (const [sessionId, entry] of notifications) {
      const activity = activities.find(item => item.is_owned === true && item.session_id === sessionId)
      if (!activity || activity.is_running || !activity.has_unread_result
        || !isTerminal(activity.completed_status) || activity.completed_message_id !== entry.messageId) {
        closeNotification(sessionId)
      }
    }
    const currentTasks = visible(activities)
    if (latestSequence !== null) {
      call(onActivityUpdated, {
        activities: activities.map(activity => ({ ...activity })),
        sequence: latestSequence
      })
    }
    if (!isCurrent(requestGeneration)) return currentTasks
    call(onTasksUpdated, currentTasks)
    if (!isCurrent(requestGeneration)) return currentTasks
    for (const activity of activities) {
      if (activity.is_owned !== true) continue
      const result = activity.completed_message_id
      const desired = readStates.get(activity.session_id)?.desired || 0
      if (activity.is_running || !activity.has_unread_result || !isTerminal(activity.completed_status)
        || result <= desired || result <= (notified.get(activity.session_id) || 0)) continue
      closeNotification(activity.session_id)
      notified.set(activity.session_id, result)
      let handle
      try { handle = onNotify(activity) } catch (error) { reportError(error) }
      if (!isCurrent(requestGeneration)) {
        closeHandle(handle, reportError)
        return currentTasks
      }
      notifications.set(activity.session_id, { messageId: result, handle })
    }
    return currentTasks
  }

  const validateActivity = value => {
    if (!isRecord(value) || !isSessionId(value.session_id)) {
      throw new TypeError('fetchActivity must return activity objects with non-empty session_id')
    }
    if (typeof value.is_owned !== 'boolean') throw new TypeError('activity is_owned must be a boolean')
    if (typeof value.is_running !== 'boolean') throw new TypeError('task is_running must be a boolean')
    if (value.completed_message_id !== null && !isPositive(value.completed_message_id)) {
      throw new TypeError('task completed_message_id must be a positive safe integer or null')
    }
    if (value.completed_status !== null && !isTerminal(value.completed_status)) {
      throw new TypeError('task completed_status must be succeeded, failed, or null')
    }
    if (value.last_read_message_id !== null && !isCursor(value.last_read_message_id)) {
      throw new TypeError('task last_read_message_id must be a safe integer or null')
    }
    return { ...value }
  }
  const pumpRead = sessionId => {
    const state = readStates.get(sessionId)
    if (!state || state.promise) return state?.promise || Promise.resolve(undefined)
    const requestGeneration = generation
    const pending = Promise.resolve().then(async () => {
      let result
      while (isCurrent(requestGeneration)) {
        if (!canReadSession(sessionId)) {
          state.desired = 0
          return result
        }
        const requested = state.desired
        if (requested <= confirmed(sessionId)) {
          state.desired = 0
          return result
        }
        try {
          const response = await markRead(sessionId, requested)
          if (!isCurrent(requestGeneration) || !canReadSession(sessionId)) return result
          const returned = response?.last_read_message_id
          if (!isRecord(response) || !isPositive(returned) || returned < requested) {
            throw new TypeError('markRead returned an invalid read cursor')
          }
          confirmedRead.set(sessionId, Math.max(confirmed(sessionId), returned))
          if (state.desired <= confirmed(sessionId)) state.desired = 0
          const nextActivities = activities.map(activity => activity.session_id === sessionId
            && activity.is_owned === true
            ? { ...activity, last_read_message_id: returned,
              ...(Object.prototype.hasOwnProperty.call(response, 'last_read_at')
                ? { last_read_at: response.last_read_at } : {}) }
            : activity)
          publish(nextActivities, requestGeneration)
          result = response
        } catch (error) {
          if (isCurrent(requestGeneration) && canReadSession(sessionId)) {
            reportError(error)
            if (permanentReadError(error)) state.desired = 0
          }
          return result
        }
      }
      return result
    })
    state.promise = pending
    pending.then(
      () => { if (state.promise === pending) state.promise = null },
      () => { if (state.promise === pending) state.promise = null }
    )
    return pending
  }

  const retryReads = () => {
    for (const [sessionId, state] of readStates) {
      if (!canReadSession(sessionId)) {
        state.desired = 0
        continue
      }
      if (!state.promise && state.desired > confirmed(sessionId)) pumpRead(sessionId)
    }
  }
  const refreshSnapshot = async () => {
    const requestGeneration = generation
    if (!isCurrent(requestGeneration)) return visible(activities)
    try {
      const requestSequence = takeSequence()
      retryReads()
      const response = await fetchActivity()
      if (!isCurrent(requestGeneration)) return visible(activities)
      if (!Array.isArray(response)) throw new TypeError('fetchActivity must return an array')
      const nextActivities = response.map(validateActivity)
      if (!isCurrent(requestGeneration)) return visible(activities)
      latestSequence = requestSequence
      return publish(nextActivities, requestGeneration)
    } catch (error) {
      if (isCurrent(requestGeneration)) reportError(error)
      return visible(activities)
    }
  }
  const makePoller = () => {
    const options = { refreshSessions: refreshSnapshot, intervalMs }
    if (typeof schedule === 'function') options.schedule = schedule
    if (typeof cancel === 'function') options.cancel = cancel
    return createSessionListPoller(options)
  }
  const setIdentity = token => {
    if (disposed) return Promise.resolve(visible(activities))
    if (token !== null && !isSessionId(token)) {
      throw new TypeError('identity must be a non-empty string or null')
    }
    if (token === identity) return Promise.resolve(visible(activities))
    if (poller) poller.dispose()
    poller = null
    generation += 1
    const changeGeneration = generation
    identity = token
    closeAllNotifications()
    activities = []
    latestSequence = null
    confirmedRead.clear()
    readStates.clear()
    notified.clear()
    call(onTasksUpdated, [])
    if (!disposed && generation === changeGeneration && identity === token) {
      call(onActivityUpdated, null)
    }
    if (disposed || generation !== changeGeneration || identity !== token || token === null) {
      return Promise.resolve(visible(activities))
    }
    poller = makePoller()
    return poller.refreshNow()
  }
  const refresh = () => {
    if (disposed || identity === null || !poller) return Promise.resolve(visible(activities))
    return poller.refreshNow()
  }
  const readSession = (sessionId, messageId) => {
    if (!isCurrent(generation) || !isSessionId(sessionId) || !isPositive(messageId)
      || !canReadSession(sessionId)) {
      return Promise.resolve(undefined)
    }
    let state = readStates.get(sessionId)
    if (!state) {
      state = { desired: 0, promise: null }
      readStates.set(sessionId, state)
    }
    if (messageId <= Math.max(state.desired, confirmed(sessionId))) {
      return state.promise || Promise.resolve(undefined)
    }
    state.desired = messageId
    return pumpRead(sessionId)
  }
  const dispose = () => {
    if (disposed) return
    disposed = true
    generation += 1
    if (poller) poller.dispose()
    poller = null
    closeAllNotifications()
    activities = []
    latestSequence = null
    confirmedRead.clear()
    readStates.clear()
    notified.clear()
    identity = null
    call(onTasksUpdated, [])
    call(onActivityUpdated, null)
  }
  return { setIdentity, refresh, readSession, dispose }
}
