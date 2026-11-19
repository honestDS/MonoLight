import assert from 'node:assert/strict'
import test from 'node:test'

import {
  createActivity,
  createDeferred,
  createHarness,
  createSession,
  flushMicrotasks,
  managerSession
} from './sessionTaskNotificationHarness.js'

test('the root activity poll feeds a mounted session manager without a second poller', async t => {
  let activity = [
    createActivity('owned-session', {
      is_running: true,
      latest_message_id: 1,
      last_active: '2026-10-08 00:00:01'
    }),
    createActivity('admin-session', {
      is_owned: false,
      is_running: true,
      latest_message_id: 10,
      last_active: '2026-10-08 00:00:02'
    }),
    createActivity('idle-session', {
      latest_message_id: null,
      last_active: '2026-10-08 00:00:03'
    })
  ]
  const sessions = [
    createSession('owned-session', { title: 'Owned full', metadata: 'owned metadata' }),
    createSession('admin-session', { title: 'Admin full', metadata: 'admin metadata' }),
    createSession('idle-session', { title: 'Idle full', metadata: 'idle metadata' })
  ]
  const harness = createHarness(t, {
    routePath: '/memories',
    fetchActivity: () => activity,
    sessionsList: () => sessions
  })

  await harness.refreshAndFlush()
  const activityCallsBeforeManager = harness.fetchCallCount()
  const timerBeforeManager = harness.timers.pending[0]
  const manager = harness.mountSessionManager()
  await manager.instance.loadSessions()
  await flushMicrotasks()

  assert.equal(harness.fetchCallCount(), activityCallsBeforeManager)
  assert.equal(harness.sessionsListCallCount(), 1)
  assert.equal(harness.timers.pending.length, 1)
  assert.equal(timerBeforeManager.delay, 1500)
  assert.deepEqual(
    Array.from(manager.instance.sessions.value, session => session.session_id),
    ['owned-session', 'admin-session', 'idle-session']
  )

  activity = [
    createActivity('owned-session', {
      completed_message_id: 5,
      completed_status: 'succeeded',
      latest_message_id: 2,
      last_active: '2026-10-08 00:00:04'
    }),
    createActivity('admin-session', {
      is_owned: false,
      is_running: false,
      latest_message_id: 11,
      last_active: '2026-10-08 00:00:05',
      is_loading: true
    }),
    createActivity('idle-session', {
      latest_message_id: null,
      last_active: '2026-10-08 00:00:03'
    })
  ]
  await harness.timers.tick()
  await flushMicrotasks()

  assert.equal(harness.fetchCallCount(), activityCallsBeforeManager + 1)
  assert.equal(harness.sessionsListCallCount(), 1)
  assert.equal(harness.timers.pending.length, 1)
  assert.equal(harness.timers.pending[0].delay, 1500)
  assert.deepEqual(Array.from(harness.instance.tasks.value, task => task.session_id), ['owned-session'])
  assert.equal(harness.notifications.length, 1)
  assert.equal(managerSession(manager, 'owned-session').latest_message_id, 2)
  assert.equal(managerSession(manager, 'admin-session').latest_message_id, 11)
  assert.equal(managerSession(manager, 'admin-session').is_loading, true)
  assert.equal(managerSession(manager, 'idle-session').title, 'Idle full')
  assert.deepEqual(
    Array.from(manager.instance.sessions.value, session => session.session_id).sort(),
    ['admin-session', 'idle-session', 'owned-session']
  )
})

test('disposing and rebuilding the manager leaves the root activity poller authoritative', async t => {
  let activity = [createActivity('web-session', {
    is_running: true,
    latest_message_id: 1,
    source: 'external'
  })]
  const sessions = [createSession('web-session', { title: 'Stable title', metadata: 'stable metadata' })]
  const harness = createHarness(t, {
    fetchActivity: () => activity,
    sessionsList: () => sessions
  })

  await harness.refreshAndFlush()
  const manager = harness.mountSessionManager()
  const oldSessionUpdates = []
  const oldActivityUpdates = []
  manager.instance.setSessionsUpdatedCallback(value => oldSessionUpdates.push(Array.from(value)))
  manager.instance.setSessionActivityUpdatedCallback(value => oldActivityUpdates.push(Array.from(value)))
  await manager.instance.loadSessions()
  await flushMicrotasks()
  const oldSessions = Array.from(manager.instance.sessions.value, session => ({ ...session }))
  const activityCallsBeforeDispose = harness.fetchCallCount()
  const sessionListCallsBeforeDispose = harness.sessionsListCallCount()
  manager.dispose()
  const callbacksAfterDispose = [oldSessionUpdates.length, oldActivityUpdates.length]

  await harness.setRoute('/memories')
  assert.equal(harness.timers.pending.length, 1)
  activity = [createActivity('web-session', {
    completed_message_id: 8,
    completed_status: 'succeeded',
    latest_message_id: 2,
    source: 'external'
  })]
  await harness.timers.tick()
  await flushMicrotasks()

  assert.equal(harness.fetchCallCount(), activityCallsBeforeDispose + 1)
  assert.equal(harness.timers.pending.length, 1)
  assert.equal(harness.notifications.length, 1)
  assert.deepEqual([oldSessionUpdates.length, oldActivityUpdates.length], callbacksAfterDispose)
  assert.deepEqual(Array.from(manager.instance.sessions.value), oldSessions)

  const activityCallsBeforeRebuild = harness.fetchCallCount()
  const rebuilt = harness.mountSessionManager()
  await rebuilt.instance.loadSessions()
  await flushMicrotasks()
  assert.equal(harness.sessionsListCallCount(), sessionListCallsBeforeDispose + 1)
  assert.equal(harness.fetchCallCount(), activityCallsBeforeRebuild)
  assert.equal(harness.timers.pending.length, 1)
  rebuilt.dispose()
})

test('hidden and visible pages keep the same 1500ms root activity interval', async t => {
  let activityVersion = 0
  const harness = createHarness(t, {
    fetchActivity: () => [createActivity('visibility-session', {
      latest_message_id: ++activityVersion
    })]
  })

  await harness.refreshAndFlush()
  const initialFetchCount = harness.fetchCallCount()
  const initialTimer = harness.timers.pending[0]
  harness.setVisibility(true)
  assert.equal(harness.document.hidden, true)
  assert.equal(harness.document.visibilityState, 'hidden')
  assert.equal(harness.timers.pending[0], initialTimer)
  assert.equal(harness.timers.pending[0].delay, 1500)
  await harness.timers.tick()
  await flushMicrotasks()
  assert.equal(harness.fetchCallCount(), initialFetchCount + 1)
  assert.equal(harness.timers.pending.length, 1)
  assert.equal(harness.timers.pending[0].delay, 1500)

  harness.setVisibility(false)
  assert.equal(harness.document.hidden, false)
  assert.equal(harness.document.visibilityState, 'visible')
  await harness.timers.tick()
  await flushMicrotasks()
  assert.equal(harness.fetchCallCount(), initialFetchCount + 2)
  assert.equal(harness.timers.pending.length, 1)
  assert.equal(harness.timers.pending[0].delay, 1500)
  assert.equal(harness.timers.scheduled.every(timer => timer.delay === 1500), true)
})

test('shared activity sequence rejects an old root snapshot and accepts the next one', async t => {
  const oldActivity = createDeferred()
  const fullList = createDeferred()
  let activity = [createActivity('shared-session', {
    latest_message_id: 300,
    last_active: '2026-10-08 00:00:03',
    source: 'external'
  })]
  const harness = createHarness(t, {
    fetchActivity: call => call === 0 ? oldActivity.promise : activity,
    sessionsList: () => fullList.promise
  })
  const manager = harness.mountSessionManager()
  const loading = manager.instance.loadSessions()
  await flushMicrotasks()
  assert.equal(harness.fetchCallCount(), 1)
  assert.equal(harness.sessionsListCallCount(), 1)

  fullList.resolve([createSession('shared-session', {
    title: 'Newer full title',
    metadata: 'newer full metadata',
    latest_message_id: 200,
    last_active: '2026-10-08 00:00:02'
  })])
  await loading
  await flushMicrotasks()
  oldActivity.resolve([createActivity('shared-session', {
    latest_message_id: 100,
    last_active: '2026-10-08 00:00:01',
    source: 'external'
  })])
  await flushMicrotasks()

  assert.equal(managerSession(manager, 'shared-session').latest_message_id, 200)
  assert.equal(managerSession(manager, 'shared-session').title, 'Newer full title')

  await harness.timers.tick()
  await flushMicrotasks()
  assert.equal(managerSession(manager, 'shared-session').latest_message_id, 300)
  assert.equal(managerSession(manager, 'shared-session').last_active, '2026-10-08 00:00:03')
})

test('a newer activity survives a pending full refresh while full metadata is retained', async t => {
  const pendingFull = createDeferred()
  let activity = [createActivity('known-session', {
    latest_message_id: 10,
    last_active: '2026-10-08 00:00:01',
    source: 'external'
  })]
  const harness = createHarness(t, {
    fetchActivity: () => activity,
    sessionsList: call => call === 1 ? pendingFull.promise : [createSession('known-session', {
      title: call === 0 ? 'Initial title' : 'Returned full title',
      metadata: call === 0 ? 'initial metadata' : 'returned full metadata',
      latest_message_id: call === 0 ? 10 : 15,
      last_active: call === 0 ? '2026-10-08 00:00:01' : '2026-10-08 00:00:02'
    })]
  })

  await harness.refreshAndFlush()
  const manager = harness.mountSessionManager()
  await manager.instance.loadSessions()
  await flushMicrotasks()
  const loading = manager.instance.loadSessions()
  await flushMicrotasks()
  assert.equal(harness.sessionsListCallCount(), 2)

  activity = [createActivity('known-session', {
    latest_message_id: 20,
    last_active: '2026-10-08 00:00:04',
    source: 'external',
    is_loading: true
  })]
  await harness.timers.tick()
  await flushMicrotasks()
  assert.equal(managerSession(manager, 'known-session').latest_message_id, 20)
  assert.equal(managerSession(manager, 'known-session').title, 'Initial title')
  assert.equal(managerSession(manager, 'known-session').metadata, 'initial metadata')

  pendingFull.resolve([createSession('known-session', {
    title: 'Returned full title',
    metadata: 'returned full metadata',
    latest_message_id: 15,
    last_active: '2026-10-08 00:00:02',
    source: 'external'
  })])
  await loading
  await flushMicrotasks()
  assert.equal(managerSession(manager, 'known-session').latest_message_id, 20)
  assert.equal(managerSession(manager, 'known-session').last_active, '2026-10-08 00:00:04')
  assert.equal(managerSession(manager, 'known-session').is_loading, true)
  assert.equal(managerSession(manager, 'known-session').title, 'Returned full title')
  assert.equal(managerSession(manager, 'known-session').metadata, 'returned full metadata')
})

test('selected web activity running-to-idle forwards activity and refreshed full snapshots', async t => {
  let activity = [createActivity('web-selected', {
    is_running: true,
    is_reply_running: true,
    latest_message_id: 1,
    source: 'http'
  })]
  const harness = createHarness(t, {
    fetchActivity: () => activity,
    sessionsList: call => [createSession('web-selected', {
      title: `full ${call}`,
      metadata: `metadata ${call}`,
      source: 'http',
      is_reply_running: call < 2
    })]
  })

  await harness.refreshAndFlush()
  const manager = harness.mountSessionManager()
  const activityUpdates = []
  const sessionUpdates = []
  manager.instance.setSessionActivityUpdatedCallback(value => activityUpdates.push(Array.from(value)))
  manager.instance.setSessionsUpdatedCallback(value => sessionUpdates.push(Array.from(value)))
  await manager.instance.loadSessions()
  manager.instance.selectSession({ session_id: 'web-selected' }, null, false, false)
  activity = [createActivity('web-selected', {
    is_running: true,
    is_reply_running: true,
    latest_message_id: 2,
    source: 'http'
  })]
  await harness.timers.tick()
  await flushMicrotasks()
  const activityUpdatesAfterRunning = activityUpdates.length
  assert.equal(harness.sessionsListCallCount(), 2)
  assert.equal(activityUpdatesAfterRunning > 0, true)

  activity = [createActivity('web-selected', {
    is_running: false,
    is_reply_running: false,
    latest_message_id: 3,
    source: 'http'
  })]
  await harness.timers.tick()
  await flushMicrotasks()

  assert.equal(harness.sessionsListCallCount(), 3)
  assert.equal(activityUpdates.length > activityUpdatesAfterRunning, true)
  assert.equal(sessionUpdates.length > 0, true)
  const latestSessionUpdate = sessionUpdates[sessionUpdates.length - 1]
  assert.equal(latestSessionUpdate.find(session => session.session_id === 'web-selected').is_reply_running, false)
})
