import assert from 'node:assert/strict'
import test from 'node:test'

import {
  createDeferred,
  createHarness,
  createTask,
  flushMicrotasks
} from './sessionTaskNotificationHarness.js'

test('protected routes restore tasks and keep one poller, while logged-out and standalone routes stay idle', async t => {
  const protectedHarness = createHarness(t, {
    routePath: '/profiles',
    token: 'token-a',
    fetchActivity: () => [createTask('profile-task', {
      title: 'Profile task',
      completed_message_id: 10,
      completed_status: 'succeeded'
    })]
  })

  await protectedHarness.refreshAndFlush()
  assert.deepEqual(protectedHarness.instance.tasks.value.map(task => task.session_id), ['profile-task'])
  assert.equal(protectedHarness.notifications.length, 1)
  const timerBeforeRouteChange = protectedHarness.timers.pending[0]
  const fetchesBeforeRouteChange = protectedHarness.fetchCallCount()

  await protectedHarness.setRoute('/memories')
  assert.equal(protectedHarness.fetchCallCount(), fetchesBeforeRouteChange)
  assert.equal(protectedHarness.timers.pending[0], timerBeforeRouteChange)
  await protectedHarness.timers.tick()
  await flushMicrotasks()
  assert.equal(protectedHarness.fetchCallCount(), fetchesBeforeRouteChange + 1)
  assert.equal(protectedHarness.notifications.length, 1)
  assert.deepEqual(protectedHarness.instance.tasks.value.map(task => task.session_id), ['profile-task'])

  const loggedOutHarness = createHarness(t, {
    routePath: '/profiles',
    token: null,
    fetchActivity: () => [createTask('should-not-load', { is_running: true })]
  })
  await flushMicrotasks()
  assert.equal(loggedOutHarness.timers.pending.length, 0)
  assert.equal(loggedOutHarness.fetchCallCount(), 0)
  assert.equal(loggedOutHarness.instance.tasks.value.length, 0)
  assert.equal(loggedOutHarness.notifications.length, 0)

  const standaloneHarness = createHarness(t, {
    routePath: '/login',
    token: 'token-standalone',
    fetchActivity: () => [createTask('should-not-load', { is_running: true })]
  })
  await flushMicrotasks()
  assert.equal(standaloneHarness.timers.pending.length, 0)
  assert.equal(standaloneHarness.fetchCallCount(), 0)
  assert.equal(standaloneHarness.instance.tasks.value.length, 0)
  assert.equal(standaloneHarness.notifications.length, 0)
})

test('identity changes discard stale responses and logout clears tasks, polling, and its notification', async t => {
  const oldResponse = createDeferred()
  const currentResponse = createDeferred()
  const lateResponse = createDeferred()
  const harness = createHarness(t, {
    routePath: '/',
    token: 'user-a',
    fetchActivity: call => {
      if (call === 0) return oldResponse.promise
      if (call === 1) return currentResponse.promise
      if (call === 2) return lateResponse.promise
      return [createTask('current-session', {
        completed_message_id: 21,
        completed_status: 'succeeded'
      })]
    }
  })
  assert.equal(harness.fetchCallCount(), 1)

  harness.setViewingSession('stale-session')
  harness.setToken('user-b')
  assert.equal(harness.fetchCallCount(), 2)
  oldResponse.resolve([createTask('stale-session', {
    completed_message_id: 20,
    completed_status: 'succeeded'
  })])
  await flushMicrotasks()
  assert.deepEqual(harness.instance.tasks.value, [])
  assert.equal(harness.notifications.length, 0)

  currentResponse.resolve([createTask('current-session', {
    completed_message_id: 21,
    completed_status: 'succeeded'
  })])
  await flushMicrotasks()
  assert.deepEqual(harness.instance.tasks.value.map(task => task.session_id), ['current-session'])
  assert.equal(harness.notifications.length, 1)
  assert.deepEqual(harness.readCalls, [])

  const timerBeforeLogout = harness.timers.pending[0]
  const lateRefresh = harness.instance.refresh()
  await flushMicrotasks()
  assert.equal(harness.fetchCallCount(), 3)
  harness.setToken(null)
  lateResponse.resolve([createTask('late-session', {
    completed_message_id: 22,
    completed_status: 'succeeded'
  })])
  await lateRefresh
  await flushMicrotasks()
  assert.deepEqual(harness.instance.tasks.value, [])
  assert.equal(harness.timers.pending.length, 0)
  assert.equal(harness.timers.cancelled.includes(timerBeforeLogout), true)
  assert.deepEqual(harness.closedNotifications.map(notification => notification.title), ['会话任务已完成'])
  assert.equal(harness.document.listenerCount('visibilitychange'), 1)
})

test('a failed fetch preserves the visible list and a later poll clears the error', async t => {
  let mode = 'success'
  const failure = new Error('temporary task failure')
  const stableTask = createTask('stable-session', { is_running: true })
  const harness = createHarness(t, {
    fetchActivity: () => mode === 'failure' ? failure : [stableTask]
  })

  await harness.refreshAndFlush()
  assert.deepEqual(harness.instance.tasks.value.map(task => task.session_id), ['stable-session'])
  assert.equal(harness.instance.error.value, false)

  mode = 'failure'
  await harness.timers.tick()
  assert.equal(harness.instance.error.value, true)
  assert.deepEqual(harness.instance.tasks.value.map(task => task.session_id), ['stable-session'])
  assert.equal(harness.timers.pending.length, 1)

  mode = 'success'
  await harness.timers.tick()
  assert.equal(harness.instance.error.value, false)
  assert.deepEqual(harness.instance.tasks.value.map(task => task.session_id), ['stable-session'])
})

test('unmount removes listeners, stops polling, and prevents a pending notification batch', async t => {
  const batchSeen = createDeferred()
  const harness = createHarness(t, {
    fetchActivity: () => [createTask('pending-notification', {
      completed_message_id: 70,
      completed_status: 'succeeded'
    })],
    onNotifyObserved: () => batchSeen.resolve()
  })
  const refresh = harness.instance.refresh()
  await batchSeen.promise
  assert.equal(harness.document.listenerCount('visibilitychange'), 1)
  harness.unmount()
  await refresh
  await flushMicrotasks()

  assert.equal(harness.notifications.length, 0)
  assert.equal(harness.window.listenerCount('storage'), 0)
  assert.equal(harness.window.listenerCount('focus'), 0)
  assert.equal(harness.document.listenerCount('visibilitychange'), 0)
  assert.equal(harness.timers.pending.length, 0)

  const fetchesAfterUnmount = harness.fetchCallCount()
  harness.window.dispatch('focus')
  harness.window.dispatch('storage', { key: 'token' })
  await flushMicrotasks()
  assert.equal(harness.fetchCallCount(), fetchesAfterUnmount)

  const pollingHarness = createHarness(t, {
    fetchActivity: () => [createTask('polling-session', { is_running: true })]
  })
  await pollingHarness.refreshAndFlush()
  const timer = pollingHarness.timers.pending[0]
  pollingHarness.unmount()
  assert.equal(pollingHarness.timers.pending.length, 0)
  assert.equal(pollingHarness.timers.cancelled.includes(timer), true)
})
