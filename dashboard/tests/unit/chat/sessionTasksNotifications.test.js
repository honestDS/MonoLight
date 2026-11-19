import assert from 'node:assert/strict'
import test from 'node:test'
import {
  createFixture,
  createTask,
  latestActivityUpdate,
  latestUpdate,
  sessionIds
} from './sessionTasksHarness.js'

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
