import assert from 'node:assert/strict'
import test from 'node:test'
import {
  createDeferred,
  flushMicrotasks,
  createTask,
  createFixture,
  latestUpdate,
  latestActivityUpdate,
  sessionIds
} from './sessionTasksHarness.js'

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
  const read = disposed.controller.readSession('dispose-session', 140)
  await flushMicrotasks()
  const updatesBeforeDispose = disposed.updates.length
  const activitiesUpdatesBeforeDispose = disposed.activitiesUpdates.length
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

test('identity changes, logout, and dispose clear automatic read state before old work completes', async () => {
  const oldAcknowledgement = createDeferred()
  const logoutSnapshot = createDeferred()
  const readRequests = []
  let fetchCount = 0
  const fixture = createFixture({
    fetchTasks: async () => {
      fetchCount += 1
      if (fetchCount === 1) {
        return [createTask('old-identity-session', {
          completed_message_id: 72,
          completed_status: 'succeeded'
        })]
      }
      if (fetchCount === 2) {
        return [createTask('new-identity-session', {
          completed_message_id: 73,
          completed_status: 'failed'
        })]
      }
      return logoutSnapshot.promise
    },
    markRead: (sessionId, messageId) => {
      readRequests.push([sessionId, messageId])
      return oldAcknowledgement.promise
    }
  })

  await fixture.controller.setIdentity('old-identity')
  fixture.controller.setViewingSession('old-identity-session')
  await flushMicrotasks()
  assert.deepEqual(readRequests, [['old-identity-session', 72]])

  await fixture.controller.setIdentity('new-identity')
  assert.deepEqual(sessionIds(latestUpdate(fixture)), ['new-identity-session'])
  assert.equal(latestActivityUpdate(fixture).activities[0].last_read_message_id, null)
  oldAcknowledgement.resolve({
    session_id: 'old-identity-session',
    last_read_message_id: 72
  })
  await flushMicrotasks()
  assert.deepEqual(sessionIds(latestUpdate(fixture)), ['new-identity-session'])
  assert.equal(latestActivityUpdate(fixture).activities[0].last_read_message_id, null)

  const staleLogoutRefresh = fixture.controller.refresh()
  await flushMicrotasks()
  await fixture.controller.setIdentity(null)
  logoutSnapshot.resolve([createTask('stale-logout-session', {
    completed_message_id: 74,
    completed_status: 'succeeded'
  })])
  await staleLogoutRefresh
  assert.deepEqual(latestUpdate(fixture), [])
  assert.equal(latestActivityUpdate(fixture), null)

  const disposeAcknowledgement = createDeferred()
  const disposed = createFixture({
    fetchTasks: async () => [createTask('dispose-view-session', {
      completed_message_id: 75,
      completed_status: 'succeeded'
    })],
    markRead: async (sessionId, messageId) => {
      readRequests.push([sessionId, messageId])
      return disposeAcknowledgement.promise
    }
  })
  await disposed.controller.setIdentity('dispose-identity')
  disposed.controller.setViewingSession('dispose-view-session')
  await flushMicrotasks()
  const updatesBeforeDispose = disposed.updates.length
  const activitiesBeforeDispose = disposed.activitiesUpdates.length
  disposed.controller.dispose()
  assert.deepEqual(latestUpdate(disposed), [])
  assert.equal(latestActivityUpdate(disposed), null)
  disposeAcknowledgement.resolve({ last_read_message_id: 75 })
  await flushMicrotasks()
  assert.equal(disposed.updates.length, updatesBeforeDispose + 1)
  assert.equal(disposed.activitiesUpdates.length, activitiesBeforeDispose + 1)
  assert.deepEqual(latestUpdate(disposed), [])
  assert.equal(latestActivityUpdate(disposed), null)
})
