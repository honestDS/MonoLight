import assert from 'node:assert/strict'
import { createSessionTaskController } from '../../../src/composables/chat/sessionTasks.js'

const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const flushMicrotasks = async () => {
  for (let index = 0; index < 8; index += 1) await Promise.resolve()
}

const createManualTimerQueue = () => {
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

const createTask = (sessionId, overrides = {}) => ({
  session_id: sessionId,
  is_owned: true,
  is_running: false,
  completed_message_id: null,
  completed_status: null,
  last_read_message_id: null,
  ...overrides
})

const createFixture = ({
  fetchTasks = async () => [],
  markRead = async (sessionId, messageId) => ({
    session_id: sessionId,
    last_read_message_id: messageId
  }),
  nextSequence
} = {}) => {
  const timers = createManualTimerQueue()
  const updates = []
  const activitiesUpdates = []
  const notifications = []
  const notificationHandles = []
  const closed = []
  const errors = []

  const controller = createSessionTaskController({
    fetchActivity: fetchTasks,
    markRead,
    onTasksUpdated: tasks => updates.push(tasks),
    onActivityUpdated: activities => activitiesUpdates.push(activities),
    onNotify: task => {
      notifications.push(task)
      let isClosed = false
      const handle = {
        close: () => {
          if (isClosed) return
          isClosed = true
          closed.push(task.session_id)
        }
      }
      notificationHandles.push({ task, handle })
      return handle
    },
    onError: error => errors.push(error),
    schedule: timers.schedule,
    cancel: timers.cancel,
    nextSequence
  })

  return {
    controller,
    timers,
    updates,
    activitiesUpdates,
    notifications,
    notificationHandles,
    closed,
    errors
  }
}

const latestUpdate = fixture => fixture.updates[fixture.updates.length - 1]
const latestActivityUpdate = fixture => fixture.activitiesUpdates[fixture.activitiesUpdates.length - 1]

const sessionIds = tasks => tasks.map(task => task.session_id)

export {
  createDeferred,
  flushMicrotasks,
  createTask,
  createFixture,
  latestUpdate,
  latestActivityUpdate,
  sessionIds
}
