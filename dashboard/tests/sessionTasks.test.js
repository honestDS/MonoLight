import assert from 'node:assert/strict'
import test from 'node:test'
import { createSessionTaskController, getSessionReadCursor } from '../src/composables/chat/sessionTasks.js'

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

test('getSessionReadCursor requires a real visible focused history at the bottom', () => {
  const messages = [
    { role: 'thinking', db_id: 100 },
    { role: 'assistant', localid: 99 },
    { role: 'user', db_id: 4 },
    { role: 'assistant', db_id: 12 },
    { role: 'tool', db_id: '20' },
    { role: 'assistant', db_id: 12.5 },
    { role: 'assistant', db_id: 0 },
    { role: 'assistant', db_id: -1 },
    { role: 'assistant', db_id: Number.MAX_SAFE_INTEGER + 1 },
    { role: 'assistant', db_id: true },
    null,
    ['not-a-message']
  ]

  assert.equal(getSessionReadCursor({
    messages,
    historyLoaded: true,
    visible: true,
    focused: true,
    atBottom: true
  }), 12)

  for (const flag of ['historyLoaded', 'visible', 'focused', 'atBottom']) {
    assert.equal(getSessionReadCursor({
      messages,
      historyLoaded: true,
      visible: true,
      focused: true,
      atBottom: true,
      [flag]: false
    }), 0)
  }

  assert.equal(getSessionReadCursor({
    messages: [],
    historyLoaded: true,
    visible: true,
    focused: true,
    atBottom: true
  }), 0)
  assert.equal(getSessionReadCursor({
    messages: null,
    historyLoaded: true,
    visible: true,
    focused: true,
    atBottom: true
  }), 0)
})

test('an initial snapshot restores completed unread results and only newer results notify again', async () => {
  const snapshots = [
    [
      createTask('succeeded', { completed_message_id: 10, completed_status: 'succeeded' }),
      createTask('failed', { completed_message_id: 20, completed_status: 'failed' })
    ],
    [
      createTask('succeeded', { completed_message_id: 10, completed_status: 'succeeded' }),
      createTask('failed', { completed_message_id: 20, completed_status: 'failed' })
    ],
    [
      createTask('succeeded', { completed_message_id: 11, completed_status: 'succeeded' }),
      createTask('failed', { completed_message_id: 21, completed_status: 'failed' })
    ]
  ]
  const fixture = createFixture({ fetchTasks: async () => snapshots.shift() || [] })

  await fixture.controller.setIdentity('user-1')
  assert.deepEqual(fixture.notifications.map(task => [
    task.session_id,
    task.completed_message_id,
    task.completed_status,
    task.has_unread_result
  ]), [
    ['succeeded', 10, 'succeeded', true],
    ['failed', 20, 'failed', true]
  ])
  assert.deepEqual(sessionIds(latestUpdate(fixture)), ['succeeded', 'failed'])

  await fixture.controller.refresh()
  assert.equal(fixture.notifications.length, 2)
  assert.deepEqual(sessionIds(latestUpdate(fixture)), ['succeeded', 'failed'])

  await fixture.controller.refresh()
  assert.deepEqual(fixture.notifications.map(task => [task.session_id, task.completed_message_id]), [
    ['succeeded', 10],
    ['failed', 20],
    ['succeeded', 11],
    ['failed', 21]
  ])
  assert.deepEqual(fixture.closed, ['succeeded', 'failed'])
  fixture.controller.dispose()
})

test('shared activity snapshots keep every row while tasks and notifications stay owned-only', async () => {
  const readRequests = []
  const idle = createTask('idle-session')
  const read = createTask('read-session', {
    completed_message_id: 10,
    completed_status: 'succeeded',
    last_read_message_id: 10
  })
  const running = createTask('running-session', { is_running: true })
  const unread = createTask('unread-session', {
    completed_message_id: 11,
    completed_status: 'failed'
  })
  const other = createTask('other-session', {
    is_owned: false,
    is_running: true,
    completed_message_id: 12,
    completed_status: 'succeeded'
  })
  const fixture = createFixture({
    fetchTasks: async () => [idle, read, running, unread, other],
    markRead: async (sessionId, messageId) => {
      readRequests.push([sessionId, messageId])
      return { session_id: sessionId, last_read_message_id: messageId }
    }
  })

  await fixture.controller.setIdentity('activity-user')
  assert.deepEqual(sessionIds(latestUpdate(fixture)), ['running-session', 'unread-session'])
  assert.deepEqual(fixture.notifications.map(task => task.session_id), ['unread-session'])
  assert.deepEqual(latestActivityUpdate(fixture), {
    activities: [
      { ...idle, has_unread_result: false },
      { ...read, has_unread_result: false },
      { ...running, has_unread_result: false },
      { ...unread, has_unread_result: true },
      other
    ],
    sequence: 1
  })

  await fixture.controller.readSession('other-session', 12)
  assert.deepEqual(readRequests, [])
  fixture.controller.dispose()
})

test('running work notifies once when it becomes a succeeded or failed terminal result', async () => {
  const running = [
    createTask('success', { is_running: true }),
    createTask('failure', { is_running: true })
  ]
  const completed = [
    createTask('success', {
      completed_message_id: 30,
      completed_status: 'succeeded'
    }),
    createTask('failure', {
      completed_message_id: 31,
      completed_status: 'failed'
    })
  ]
  const snapshots = [running, completed, completed]
  const fixture = createFixture({ fetchTasks: async () => snapshots.shift() || [] })

  await fixture.controller.setIdentity('user-2')
  assert.equal(fixture.notifications.length, 0)
  assert.deepEqual(sessionIds(latestUpdate(fixture)), ['success', 'failure'])

  await fixture.controller.refresh()
  assert.deepEqual(fixture.notifications.map(task => [
    task.session_id,
    task.completed_status,
    task.is_running,
    task.has_unread_result
  ]), [
    ['success', 'succeeded', false, true],
    ['failure', 'failed', false, true]
  ])

  await fixture.controller.refresh()
  assert.equal(fixture.notifications.length, 2)
  fixture.controller.dispose()
})

test('running work with an old unread result waits for a terminal snapshot before notifying', async () => {
  const oldUnreadRunning = createTask('session-a', {
    is_running: true,
    completed_message_id: 40,
    completed_status: 'succeeded'
  })
  const snapshots = [
    [oldUnreadRunning],
    [oldUnreadRunning],
    [{ ...oldUnreadRunning, is_running: false }]
  ]
  const fixture = createFixture({ fetchTasks: async () => snapshots.shift() || [] })

  await fixture.controller.setIdentity('user-3')
  assert.equal(fixture.notifications.length, 0)
  assert.equal(latestUpdate(fixture)[0].has_unread_result, true)

  await fixture.controller.refresh()
  assert.equal(fixture.notifications.length, 0)

  await fixture.controller.refresh()
  assert.equal(fixture.notifications.length, 1)
  assert.equal(fixture.notifications[0].completed_message_id, 40)
  fixture.controller.dispose()
})

test('closing a toast does not mark its completed result as read', async () => {
  const snapshot = [createTask('toast-session', {
    completed_message_id: 50,
    completed_status: 'succeeded'
  })]
  const fixture = createFixture({ fetchTasks: async () => snapshot })

  await fixture.controller.setIdentity('user-4')
  fixture.notificationHandles[0].handle.close()
  assert.deepEqual(fixture.closed, ['toast-session'])

  await fixture.controller.refresh()
  assert.equal(fixture.notifications.length, 1)
  assert.equal(latestUpdate(fixture)[0].has_unread_result, true)
  fixture.controller.dispose()
})

test('reading a completed result removes it while preserving a still-running session', async () => {
  const readRequests = []
  const replacement = createFixture({
    fetchTasks: async () => [
      createTask('completed-session', {
        completed_message_id: 60,
        completed_status: 'failed'
      }),
      createTask('running-session', { is_running: true })
    ],
    markRead: async (sessionId, messageId) => {
      readRequests.push([sessionId, messageId])
      return { last_read_message_id: messageId }
    }
  })

  await replacement.controller.setIdentity('user-5')
  assert.deepEqual(sessionIds(latestUpdate(replacement)), [
    'completed-session',
    'running-session'
  ])
  await replacement.controller.readSession('completed-session', 60)
  assert.deepEqual(readRequests, [['completed-session', 60]])
  assert.deepEqual(sessionIds(latestUpdate(replacement)), ['running-session'])
  assert.deepEqual(replacement.closed, ['completed-session'])
  replacement.controller.dispose()
})

test('deleting a task and then receiving an empty snapshot converges and closes its notification', async () => {
  const running = createTask('running-session', { is_running: true })
  const snapshots = [
    [
      createTask('deleted-session', {
        completed_message_id: 70,
        completed_status: 'succeeded'
      }),
      running
    ],
    [running],
    []
  ]
  const fixture = createFixture({ fetchTasks: async () => snapshots.shift() || [] })

  await fixture.controller.setIdentity('user-6')
  assert.equal(fixture.notifications.length, 1)

  await fixture.controller.refresh()
  assert.deepEqual(sessionIds(latestUpdate(fixture)), ['running-session'])
  assert.deepEqual(fixture.closed, ['deleted-session'])

  await fixture.controller.refresh()
  assert.deepEqual(latestUpdate(fixture), [])
  assert.deepEqual(fixture.closed, ['deleted-session'])
  fixture.controller.dispose()
})

test('a server read cursor synchronizes across clients and remains confirmed on later snapshots', async () => {
  const snapshots = [
    [createTask('shared-session', {
      completed_message_id: 80,
      completed_status: 'succeeded'
    })],
    [createTask('shared-session', {
      completed_message_id: 80,
      completed_status: 'succeeded',
      last_read_message_id: 80
    })],
    [createTask('shared-session', {
      completed_message_id: 80,
      completed_status: 'succeeded'
    })]
  ]
  const fixture = createFixture({ fetchTasks: async () => snapshots.shift() || [] })

  await fixture.controller.setIdentity('user-7')
  assert.equal(fixture.notifications.length, 1)

  await fixture.controller.refresh()
  assert.deepEqual(latestUpdate(fixture), [])
  assert.deepEqual(fixture.closed, ['shared-session'])

  await fixture.controller.refresh()
  assert.deepEqual(latestUpdate(fixture), [])
  assert.equal(fixture.notifications.length, 1)
  fixture.controller.dispose()
})

test('polling keeps one 1500 timer, shares its start sequence with reads, and preserves the highest cursor', async () => {
  const pollingSnapshot = createDeferred()
  let clock = 100
  let fetchIndex = 0
  const fixture = createFixture({
    fetchTasks: async () => [
      [createTask('sequence-session', {
        completed_message_id: 80,
        completed_status: 'succeeded'
      })],
      pollingSnapshot.promise,
      [createTask('sequence-session', {
        completed_message_id: 80,
        completed_status: 'succeeded',
        last_read_message_id: 1
      })]
    ][fetchIndex++],
    markRead: async (sessionId, messageId) => ({
      session_id: sessionId,
      last_read_message_id: messageId
    }),
    nextSequence: () => clock
  })

  await fixture.controller.setIdentity('sequence-user')
  assert.equal(latestActivityUpdate(fixture).sequence, 100)
  assert.equal(fixture.timers.pending.length, 1)
  assert.equal(fixture.timers.pending[0].delay, 1500)

  clock = 200
  const polling = fixture.timers.tick()
  await flushMicrotasks()
  assert.equal(fixture.timers.pending.length, 0)
  clock = 300
  pollingSnapshot.resolve([createTask('sequence-session', {
    completed_message_id: 80,
    completed_status: 'succeeded'
  })])
  await polling
  assert.equal(latestActivityUpdate(fixture).sequence, 200)
  assert.equal(fixture.timers.pending.length, 1)
  assert.equal(fixture.timers.pending[0].delay, 1500)

  await fixture.controller.readSession('sequence-session', 80)
  assert.equal(latestActivityUpdate(fixture).sequence, 200)
  assert.equal(latestActivityUpdate(fixture).activities[0].last_read_message_id, 80)
  assert.equal(latestActivityUpdate(fixture).activities[0].has_unread_result, false)
  assert.deepEqual(latestUpdate(fixture), [])

  clock = 400
  await fixture.controller.refresh()
  assert.equal(latestActivityUpdate(fixture).sequence, 400)
  assert.equal(latestActivityUpdate(fixture).activities[0].last_read_message_id, 80)
  assert.equal(latestActivityUpdate(fixture).activities[0].has_unread_result, false)
  assert.equal(fixture.timers.pending.length, 1)
  fixture.controller.dispose()
})

test('a read for an unknown session can finish before the first snapshot and suppresses its later notification', async () => {
  const firstSnapshot = createDeferred()
  const readRequests = []
  const fixture = createFixture({
    fetchTasks: async () => firstSnapshot.promise,
    markRead: async (sessionId, messageId) => {
      readRequests.push([sessionId, messageId])
      return { session_id: sessionId, last_read_message_id: messageId }
    }
  })

  const identity = fixture.controller.setIdentity('pending-user')
  await flushMicrotasks()
  await fixture.controller.readSession('new-session', 210)
  assert.deepEqual(readRequests, [['new-session', 210]])

  firstSnapshot.resolve([createTask('new-session', {
    completed_message_id: 210,
    completed_status: 'succeeded'
  })])
  await identity
  assert.deepEqual(latestUpdate(fixture), [])
  assert.deepEqual(fixture.notifications, [])
  assert.deepEqual(latestActivityUpdate(fixture).activities, [
    createTask('new-session', {
      completed_message_id: 210,
      completed_status: 'succeeded',
      last_read_message_id: 210,
      has_unread_result: false
    })
  ])
  fixture.controller.dispose()
})

test('a late snapshot that started before markRead cannot revive an already-read result', async () => {
  const staleSnapshot = createDeferred()
  const readResponse = createDeferred()
  let fetchCount = 0
  let fetchStartedBeforeRead = false
  let markReadStarted = false
  const fixture = createFixture({
    fetchTasks: async () => {
      fetchCount += 1
      if (fetchCount === 1) return [createTask('race-session', {
        completed_message_id: 90,
        completed_status: 'succeeded'
      })]
      fetchStartedBeforeRead = true
      return staleSnapshot.promise
    },
    markRead: async () => {
      markReadStarted = true
      return readResponse.promise
    }
  })

  await fixture.controller.setIdentity('user-8')
  const lateRefresh = fixture.controller.refresh()
  await flushMicrotasks()
  assert.equal(fetchStartedBeforeRead, true)

  const read = fixture.controller.readSession('race-session', 90)
  await flushMicrotasks()
  assert.equal(markReadStarted, true)
  readResponse.resolve({ last_read_message_id: 90 })
  await read
  assert.deepEqual(latestUpdate(fixture), [])

  staleSnapshot.resolve([createTask('race-session', {
    completed_message_id: 90,
    completed_status: 'succeeded'
  })])
  await lateRefresh
  assert.deepEqual(latestUpdate(fixture), [])
  assert.equal(fixture.notifications.length, 1)
  fixture.controller.dispose()
})

test('a failed markRead is not optimistic and is retried by the next polling round', async () => {
  const failure = new Error('temporary read failure')
  let attempt = 0
  const fixture = createFixture({
    fetchTasks: async () => [createTask('retry-session', {
      completed_message_id: 100,
      completed_status: 'failed'
    })],
    markRead: async (sessionId, messageId) => {
      attempt += 1
      if (attempt === 1) throw failure
      return { session_id: sessionId, last_read_message_id: messageId }
    }
  })

  await fixture.controller.setIdentity('user-9')
  await fixture.controller.readSession('retry-session', 100)
  assert.equal(attempt, 1)
  assert.equal(latestUpdate(fixture)[0].has_unread_result, true)
  assert.equal(latestUpdate(fixture)[0].last_read_message_id, null)
  assert.deepEqual(fixture.errors, [failure])
  assert.equal(fixture.timers.pending.length, 1)

  await fixture.timers.tick()
  await flushMicrotasks()
  assert.equal(attempt, 2)
  assert.deepEqual(latestUpdate(fixture), [])
  assert.equal(fixture.timers.pending.length, 1)
  fixture.controller.dispose()
})

test('concurrent reads for one session serialize, merge the highest cursor, and ignore old cursors', async () => {
  const firstResponse = createDeferred()
  const secondResponse = createDeferred()
  const requests = []
  const fixture = createFixture({
    fetchTasks: async () => [createTask('read-session', { is_running: true })],
    markRead: (sessionId, messageId) => {
      requests.push([sessionId, messageId])
      return messageId === 5 ? firstResponse.promise : secondResponse.promise
    }
  })

  await fixture.controller.setIdentity('user-10')
  const firstRead = fixture.controller.readSession('read-session', 5)
  await flushMicrotasks()
  const mergedRead = fixture.controller.readSession('read-session', 8)
  assert.deepEqual(requests, [['read-session', 5]])

  firstResponse.resolve({ last_read_message_id: 5 })
  await flushMicrotasks()
  assert.deepEqual(requests, [
    ['read-session', 5],
    ['read-session', 8]
  ])

  secondResponse.resolve({ last_read_message_id: 8 })
  await Promise.all([firstRead, mergedRead])
  await fixture.controller.readSession('read-session', 7)
  assert.deepEqual(requests, [
    ['read-session', 5],
    ['read-session', 8]
  ])
  fixture.controller.dispose()
})

test('a smaller markRead cursor is rejected without falsely marking the result read', async () => {
  const errors = []
  const fixture = createFixture({
    fetchTasks: async () => [createTask('short-cursor', {
      completed_message_id: 110,
      completed_status: 'succeeded'
    })],
    markRead: async () => ({ last_read_message_id: 109 })
  })

  await fixture.controller.setIdentity('user-11')
  await fixture.controller.readSession('short-cursor', 110)
  errors.push(...fixture.errors)
  assert.equal(errors.length, 1)
  assert.equal(errors[0] instanceof TypeError, true)
  assert.equal(latestUpdate(fixture)[0].has_unread_result, true)
  assert.equal(latestUpdate(fixture)[0].last_read_message_id, null)
  fixture.controller.dispose()
})

test('an invalid is_owned response preserves the previous complete shared snapshot without publishing', async () => {
  let fetchCount = 0
  const fixture = createFixture({
    fetchTasks: async () => {
      fetchCount += 1
      if (fetchCount === 1) {
        return [
          createTask('valid-owned', {
            completed_message_id: 115,
            completed_status: 'succeeded'
          }),
          createTask('valid-other', { is_owned: false, is_running: true })
        ]
      }
      return [createTask('invalid-owned', { is_owned: 'unknown', is_running: true })]
    }
  })

  await fixture.controller.setIdentity('validating-user')
  const originalActivitiesUpdate = latestActivityUpdate(fixture)
  const activityUpdateCount = fixture.activitiesUpdates.length
  const originalTasksUpdate = latestUpdate(fixture)

  await fixture.controller.refresh()
  assert.equal(fixture.errors.length, 1)
  assert.equal(fixture.errors[0] instanceof TypeError, true)
  assert.equal(fixture.activitiesUpdates.length, activityUpdateCount)
  assert.deepEqual(latestActivityUpdate(fixture), originalActivitiesUpdate)
  assert.deepEqual(latestUpdate(fixture), originalTasksUpdate)
  fixture.controller.dispose()
})

test('fetch failures and malformed snapshots preserve the state while polling continues', async () => {
  const failure = new Error('temporary task fetch failure')
  const snapshots = [
    [createTask('stable-session', {
      completed_message_id: 120,
      completed_status: 'succeeded'
    })],
    failure,
    [createTask('stable-session', { completed_message_id: 0 })],
    []
  ]
  const fixture = createFixture({
    fetchTasks: async () => {
      const next = snapshots.shift()
      if (next instanceof Error) throw next
      return next
    }
  })

  await fixture.controller.setIdentity('user-12')
  const originalUpdate = latestUpdate(fixture)
  assert.deepEqual(sessionIds(originalUpdate), ['stable-session'])

  await fixture.timers.tick()
  assert.equal(fixture.errors[0], failure)
  assert.deepEqual(sessionIds(latestUpdate(fixture)), ['stable-session'])
  assert.equal(fixture.timers.pending.length, 1)

  await fixture.timers.tick()
  assert.equal(fixture.errors[1] instanceof TypeError, true)
  assert.deepEqual(sessionIds(latestUpdate(fixture)), ['stable-session'])
  assert.equal(fixture.timers.pending.length, 1)

  await fixture.timers.tick()
  assert.deepEqual(latestUpdate(fixture), [])
  assert.equal(fixture.timers.pending.length, 1)
  fixture.controller.dispose()
})

test('identity changes, logout, and dispose invalidate late work; the same identity does not rebuild polling', async () => {
  const firstIdentityFetch = createDeferred()
  const secondIdentityFetch = createDeferred()
  const logoutFetch = createDeferred()
  let fetchCount = 0
  const fixture = createFixture({
    fetchTasks: async () => {
      fetchCount += 1
      if (fetchCount === 1) return firstIdentityFetch.promise
      if (fetchCount === 2) return secondIdentityFetch.promise
      return logoutFetch.promise
    }
  })

  const firstIdentity = fixture.controller.setIdentity('user-a')
  const secondIdentity = fixture.controller.setIdentity('user-b')
  assert.equal(fetchCount, 2)
  assert.equal(fixture.timers.pending.length, 0)

  firstIdentityFetch.resolve([createTask('stale-a', {
    completed_message_id: 130,
    completed_status: 'succeeded'
  })])
  await firstIdentity
  assert.equal(fixture.notifications.length, 0)
  assert.equal(fixture.updates.some(tasks => tasks.some(task => task.session_id === 'stale-a')), false)
  assert.equal(latestActivityUpdate(fixture), null)
  assert.equal(fixture.activitiesUpdates.some(update => update?.activities
    ?.some(activity => activity.session_id === 'stale-a')), false)

  secondIdentityFetch.resolve([createTask('current-b', { is_running: true })])
  await secondIdentity
  assert.deepEqual(sessionIds(latestUpdate(fixture)), ['current-b'])
  assert.deepEqual(sessionIds(latestActivityUpdate(fixture).activities), ['current-b'])
  const currentTimer = fixture.timers.pending[0]
  const fetchesBeforeSameIdentity = fetchCount
  const sameIdentityResult = await fixture.controller.setIdentity('user-b')
  assert.equal(fetchCount, fetchesBeforeSameIdentity)
  assert.equal(fixture.timers.pending[0], currentTimer)
  assert.deepEqual(sessionIds(sameIdentityResult), ['current-b'])

  const lateLogoutRefresh = fixture.controller.refresh()
  assert.equal(fetchCount, 3)
  await fixture.controller.setIdentity(null)
  assert.equal(fixture.timers.pending.length, 0)
  logoutFetch.resolve([createTask('stale-after-logout', {
    completed_message_id: 131,
    completed_status: 'succeeded'
  })])
  await lateLogoutRefresh
  assert.equal(fixture.notifications.length, 0)
  assert.deepEqual(latestUpdate(fixture), [])
  assert.equal(latestActivityUpdate(fixture), null)
  assert.equal(fixture.activitiesUpdates.some(update => update?.activities
    ?.some(activity => activity.session_id === 'stale-after-logout')), false)

  const lateReadResponse = createDeferred()
  const disposed = createFixture({
    fetchTasks: async () => [createTask('dispose-session', {
      completed_message_id: 140,
      completed_status: 'succeeded'
    })],
    markRead: async () => lateReadResponse.promise
  })
  await disposed.controller.setIdentity('user-dispose')
  assert.equal(disposed.notifications.length, 1)
  const updatesBeforeDispose = disposed.updates.length
  const activitiesUpdatesBeforeDispose = disposed.activitiesUpdates.length
  const read = disposed.controller.readSession('dispose-session', 140)
  await flushMicrotasks()
  disposed.controller.dispose()
  assert.equal(disposed.timers.pending.length, 0)
  assert.equal(disposed.updates.length, updatesBeforeDispose + 1)
  assert.equal(disposed.activitiesUpdates.length, activitiesUpdatesBeforeDispose + 1)
  assert.deepEqual(latestUpdate(disposed), [])
  assert.equal(latestActivityUpdate(disposed), null)
  const updatesAfterDispose = disposed.updates.length
  const activitiesUpdatesAfterDispose = disposed.activitiesUpdates.length
  lateReadResponse.resolve({ last_read_message_id: 140 })
  await read
  assert.equal(disposed.updates.length, updatesAfterDispose)
  assert.equal(disposed.activitiesUpdates.length, activitiesUpdatesAfterDispose)
  assert.equal(disposed.notifications.length, 1)
})

test('business read refusals stop retries while preserving unread results and polling', async () => {
  for (const code of [400, 403, 404]) {
    const readRequests = []
    const refusal = new Error(`read refused with business code ${code}`)
    refusal.response = { status: 200, data: { code } }
    const fixture = createFixture({
      fetchTasks: async () => [createTask(`refused-${code}`, {
        completed_message_id: 150 + code,
        completed_status: 'failed'
      })],
      markRead: async (sessionId, messageId) => {
        readRequests.push([sessionId, messageId])
        throw refusal
      }
    })

    await fixture.controller.setIdentity(`user-refused-${code}`)
    assert.deepEqual(latestUpdate(fixture), [
      createTask(`refused-${code}`, {
        completed_message_id: 150 + code,
        completed_status: 'failed',
        has_unread_result: true
      })
    ])
    assert.equal(fixture.timers.pending.length, 1)

    await fixture.controller.readSession(`refused-${code}`, 150 + code)
    assert.deepEqual(readRequests, [[`refused-${code}`, 150 + code]])
    assert.deepEqual(fixture.errors, [refusal])
    assert.equal(latestUpdate(fixture)[0].has_unread_result, true)

    await fixture.timers.tick()
    assert.deepEqual(readRequests, [[`refused-${code}`, 150 + code]])
    assert.equal(latestUpdate(fixture)[0].has_unread_result, true)
    assert.equal(fixture.timers.pending.length, 1)
    fixture.controller.dispose()
  }
})
