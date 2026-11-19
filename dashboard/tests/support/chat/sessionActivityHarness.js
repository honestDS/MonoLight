import assert from 'node:assert/strict'

import { createSessionActivityController } from '../../../src/composables/chat/sessionActivity.js'

export const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

export const flushMicrotasks = async (turns = 6) => {
  for (let index = 0; index < turns; index += 1) {
    await Promise.resolve()
  }
}

export const createManualTimerQueue = () => {
  const pending = []
  const scheduled = []
  const cancelled = []

  const schedule = (callback, delay) => {
    const handle = { callback, delay }
    pending.push(handle)
    scheduled.push(handle)
    return handle
  }

  const cancel = handle => {
    cancelled.push(handle)
    const index = pending.indexOf(handle)
    if (index !== -1) pending.splice(index, 1)
  }

  const tick = async () => {
    const handle = pending.shift()
    assert.ok(handle, 'expected a scheduled timer')
    return handle.callback()
  }

  return { pending, scheduled, cancelled, schedule, cancel, tick }
}

export const makeSession = (sessionId, overrides = {}) => ({
  session_id: sessionId,
  title: `Title ${sessionId}`,
  config: { model: 'model-1', temperature: 0.1 },
  reply_works: [{ work_id: `work-${sessionId}`, status: 'succeeded' }],
  uid: `uid-${sessionId}`,
  latest_message_id: 1,
  last_active: '2026-10-08 00:00:00',
  source: 'local',
  is_reply_running: false,
  ...overrides
})

export const makeActivity = (sessionId, overrides = {}) => ({
  session_id: sessionId,
  latest_message_id: 2,
  last_active: '2026-10-08 00:01:00',
  source: 'local',
  is_reply_running: false,
  ...overrides
})

export const createControllerHarness = ({
  initialSessions = [],
  currentSessionId = null,
  fetchSessions = async () => [],
  fetchActivity = async () => [],
  onSessionsUpdated,
  onActivityUpdated,
  onError,
  afterSetSessions
} = {}) => {
  let sessions = initialSessions
  let selectedSessionId = currentSessionId
  const writes = []
  const sessionsUpdated = []
  const activitiesUpdated = []
  const errors = []

  const controller = createSessionActivityController({
    fetchSessions,
    fetchActivity,
    getSessions: () => sessions,
    setSessions: value => {
      sessions = value
      writes.push(value)
      afterSetSessions?.(value)
    },
    getCurrentSessionId: () => selectedSessionId,
    onSessionsUpdated: value => {
      sessionsUpdated.push(value)
      return onSessionsUpdated?.(value)
    },
    onActivityUpdated: value => {
      activitiesUpdated.push(value)
      return onActivityUpdated?.(value)
    },
    onError: error => {
      errors.push(error)
      return onError?.(error)
    }
  })

  return {
    controller,
    getSessions: () => sessions,
    setCurrentSessionId: value => {
      selectedSessionId = value
    },
    writes,
    sessionsUpdated,
    activitiesUpdated,
    errors
  }
}
