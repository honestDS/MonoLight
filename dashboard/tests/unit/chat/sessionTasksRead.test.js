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

test('viewing an existing completed result hides it before read acknowledgement', async () => {
  const acknowledgement = createDeferred()
  const readRequests = []
  const fixture = createFixture({
    fetchTasks: async () => [createTask('viewed-session', {
      completed_message_id: 61,
      completed_status: 'succeeded'
    })],
    markRead: async (sessionId, messageId) => {
      readRequests.push([sessionId, messageId])
      return acknowledgement.promise
    }
  })

  await fixture.controller.setIdentity('view-user')
  assert.deepEqual(sessionIds(latestUpdate(fixture)), ['viewed-session'])
  assert.equal(fixture.notifications.length, 1)

  fixture.controller.setViewingSession('viewed-session')
  await flushMicrotasks()
  assert.deepEqual(readRequests, [['viewed-session', 61]])
  assert.deepEqual(latestUpdate(fixture), [])
  assert.equal(latestActivityUpdate(fixture).activities[0].has_unread_result, false)
  assert.equal(latestActivityUpdate(fixture).activities[0].last_read_message_id, null)
  assert.deepEqual(fixture.closed, ['viewed-session'])

  acknowledgement.resolve({ session_id: 'viewed-session', last_read_message_id: 61 })
  await flushMicrotasks()
  assert.equal(latestActivityUpdate(fixture).activities[0].last_read_message_id, 61)
  fixture.controller.dispose()
})

test('a view registered before the first snapshot auto-reads its result without notifying it', async () => {
  for (const scenario of [
    { name: 'success', error: null },
    { name: 'failure', error: new Error('automatic read failed') }
  ]) {
    const firstSnapshot = createDeferred()
    const readRequests = []
    const fixture = createFixture({
      fetchTasks: async () => firstSnapshot.promise,
      markRead: async (sessionId, messageId) => {
        readRequests.push([sessionId, messageId])
        if (scenario.error) throw scenario.error
        return { session_id: sessionId, last_read_message_id: messageId }
      }
    })

    const identity = fixture.controller.setIdentity(`view-before-${scenario.name}`)
    await flushMicrotasks()
    fixture.controller.setViewingSession('current-session')
    firstSnapshot.resolve([
      createTask('current-session', {
        completed_message_id: 62,
        completed_status: 'failed'
      }),
      createTask('background-session', {
        completed_message_id: 63,
        completed_status: 'succeeded'
      })
    ])
    await identity
    await flushMicrotasks()

    assert.deepEqual(readRequests, [['current-session', 62]])
    assert.deepEqual(sessionIds(latestUpdate(fixture)), ['background-session'])
    assert.deepEqual(fixture.notifications.map(task => task.session_id), ['background-session'])
    const current = latestActivityUpdate(fixture).activities
      .find(activity => activity.session_id === 'current-session')
    assert.equal(current.has_unread_result, false)
    assert.equal(current.last_read_message_id, scenario.error ? null : 62)
    if (scenario.error) assert.deepEqual(fixture.errors, [scenario.error])
    fixture.controller.dispose()
  }
})

test('viewing an old running result reads it without hiding work and ignores stale snapshots after leaving', async () => {
  const running = createTask('running-view', {
    is_running: true,
    completed_message_id: 64,
    completed_status: 'succeeded'
  })
  const higher = createTask('running-view', {
    completed_message_id: 65,
    completed_status: 'failed'
  })
  const newer = createTask('running-view', {
    completed_message_id: 66,
    completed_status: 'succeeded'
  })
  const snapshots = [[running], [higher], [newer], [higher], [newer]]
  const readRequests = []
  const fixture = createFixture({
    fetchTasks: async () => snapshots.shift() || [],
    markRead: async (sessionId, messageId) => {
      readRequests.push([sessionId, messageId])
      return { session_id: sessionId, last_read_message_id: messageId }
    }
  })

  await fixture.controller.setIdentity('running-view-user')
  fixture.controller.setViewingSession('running-view')
  await flushMicrotasks()
  assert.deepEqual(readRequests, [['running-view', 64]])
  assert.deepEqual(sessionIds(latestUpdate(fixture)), ['running-view'])
  assert.equal(latestActivityUpdate(fixture).activities[0].has_unread_result, false)
  assert.equal(latestActivityUpdate(fixture).activities[0].last_read_message_id, 64)

  await fixture.controller.refresh()
  await flushMicrotasks()
  assert.deepEqual(readRequests, [
    ['running-view', 64],
    ['running-view', 65]
  ])
  assert.deepEqual(latestUpdate(fixture), [])
  assert.equal(latestActivityUpdate(fixture).activities[0].last_read_message_id, 65)

  fixture.controller.setViewingSession(null)
  await fixture.controller.refresh()
  assert.deepEqual(sessionIds(latestUpdate(fixture)), ['running-view'])
  assert.equal(latestUpdate(fixture)[0].has_unread_result, true)
  assert.deepEqual(fixture.notifications.map(task => [task.session_id, task.completed_message_id]), [
    ['running-view', 66]
  ])

  await fixture.controller.refresh()
  assert.deepEqual(latestUpdate(fixture), [])
  assert.equal(fixture.notifications.length, 1)
  await fixture.controller.refresh()
  assert.deepEqual(sessionIds(latestUpdate(fixture)), ['running-view'])
  assert.equal(fixture.notifications.length, 1)
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

test('automatic reads serialize the highest cursor and ignore duplicate or old snapshots', async () => {
  const firstSnapshot = createDeferred()
  const firstAcknowledgement = createDeferred()
  const secondAcknowledgement = createDeferred()
  const readRequests = []
  let fetchCount = 0
  const fixture = createFixture({
    fetchTasks: async () => {
      fetchCount += 1
      if (fetchCount === 1) return firstSnapshot.promise
      if (fetchCount === 2 || fetchCount === 4) {
        return [createTask('merge-session', {
          completed_message_id: 68,
          completed_status: 'succeeded'
        })]
      }
      return [createTask('merge-session', {
        completed_message_id: 67,
        completed_status: 'succeeded'
      })]
    },
    markRead: (sessionId, messageId) => {
      readRequests.push([sessionId, messageId])
      return messageId === 67
        ? firstAcknowledgement.promise
        : secondAcknowledgement.promise
    }
  })

  const identity = fixture.controller.setIdentity('merge-user')
  await flushMicrotasks()
  fixture.controller.setViewingSession('merge-session')
  firstSnapshot.resolve([createTask('merge-session', {
    completed_message_id: 67,
    completed_status: 'succeeded'
  })])
  await identity
  await flushMicrotasks()
  assert.deepEqual(readRequests, [['merge-session', 67]])

  await fixture.controller.refresh()
  assert.deepEqual(readRequests, [['merge-session', 67]])
  await fixture.controller.refresh()
  assert.deepEqual(readRequests, [['merge-session', 67]])
  await fixture.controller.refresh()
  assert.deepEqual(readRequests, [['merge-session', 67]])
  assert.deepEqual(latestUpdate(fixture), [])

  firstAcknowledgement.resolve({ session_id: 'merge-session', last_read_message_id: 67 })
  await flushMicrotasks()
  assert.deepEqual(readRequests, [
    ['merge-session', 67],
    ['merge-session', 68]
  ])

  secondAcknowledgement.resolve({ session_id: 'merge-session', last_read_message_id: 68 })
  await flushMicrotasks()
  assert.deepEqual(latestUpdate(fixture), [])
  assert.equal(latestActivityUpdate(fixture).activities[0].has_unread_result, false)
  assert.equal(latestActivityUpdate(fixture).activities[0].last_read_message_id, 68)
  fixture.controller.dispose()
})

test('a failed automatic read retries after leaving while the local result stays read', async () => {
  const failure = new Error('temporary automatic read failure')
  let attempt = 0
  const readRequests = []
  const fixture = createFixture({
    fetchTasks: async () => [createTask('retry-view', {
      completed_message_id: 69,
      completed_status: 'failed'
    })],
    markRead: async (sessionId, messageId) => {
      attempt += 1
      readRequests.push([sessionId, messageId])
      if (attempt === 1) throw failure
      return { session_id: sessionId, last_read_message_id: messageId }
    }
  })

  await fixture.controller.setIdentity('retry-view-user')
  fixture.controller.setViewingSession('retry-view')
  await flushMicrotasks()
  assert.deepEqual(readRequests, [['retry-view', 69]])
  assert.deepEqual(latestUpdate(fixture), [])
  assert.equal(latestActivityUpdate(fixture).activities[0].has_unread_result, false)
  assert.equal(latestActivityUpdate(fixture).activities[0].last_read_message_id, null)
  assert.deepEqual(fixture.errors, [failure])

  fixture.controller.setViewingSession(null)
  await fixture.timers.tick()
  await flushMicrotasks()
  assert.deepEqual(readRequests, [
    ['retry-view', 69],
    ['retry-view', 69]
  ])
  assert.deepEqual(latestUpdate(fixture), [])
  assert.equal(latestActivityUpdate(fixture).activities[0].has_unread_result, false)
  assert.equal(latestActivityUpdate(fixture).activities[0].last_read_message_id, 69)
  fixture.controller.dispose()
})

test('unknown, unowned, and null viewing sessions do not auto-read and invalid ids are rejected', async () => {
  const readRequests = []
  const fixture = createFixture({
    fetchTasks: async () => [
      createTask('owned-result', {
        completed_message_id: 70,
        completed_status: 'succeeded'
      }),
      createTask('other-result', {
        is_owned: false,
        completed_message_id: 71,
        completed_status: 'failed'
      })
    ],
    markRead: async (sessionId, messageId) => {
      readRequests.push([sessionId, messageId])
      return { session_id: sessionId, last_read_message_id: messageId }
    }
  })

  await fixture.controller.setIdentity('viewing-user')
  for (const sessionId of ['missing-result', 'other-result', null]) {
    fixture.controller.setViewingSession(sessionId)
    await fixture.controller.refresh()
  }

  assert.deepEqual(readRequests, [])
  assert.deepEqual(sessionIds(latestUpdate(fixture)), ['owned-result'])
  assert.equal(latestUpdate(fixture)[0].has_unread_result, true)
  assert.deepEqual(fixture.notifications.map(task => task.session_id), ['owned-result'])
  assert.throws(() => fixture.controller.setViewingSession(''), TypeError)
  assert.throws(() => fixture.controller.setViewingSession('   '), TypeError)
  assert.throws(() => fixture.controller.setViewingSession(123), TypeError)
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

test('a failed markRead keeps the result locally read and retries by the next polling round', async () => {
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
  assert.deepEqual(latestUpdate(fixture), [])
  assert.equal(latestActivityUpdate(fixture).activities[0].has_unread_result, false)
  assert.equal(latestActivityUpdate(fixture).activities[0].last_read_message_id, null)
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

test('a smaller markRead cursor keeps the result locally read without server confirmation', async () => {
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
  assert.deepEqual(latestUpdate(fixture), [])
  assert.equal(latestActivityUpdate(fixture).activities[0].has_unread_result, false)
  assert.equal(latestActivityUpdate(fixture).activities[0].last_read_message_id, null)
  fixture.controller.dispose()
})

test('business read refusals stop retries while preserving local read state and polling', async () => {
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
    assert.deepEqual(latestUpdate(fixture), [])
    assert.equal(latestActivityUpdate(fixture).activities[0].has_unread_result, false)
    assert.equal(latestActivityUpdate(fixture).activities[0].last_read_message_id, null)

    await fixture.timers.tick()
    assert.deepEqual(readRequests, [[`refused-${code}`, 150 + code]])
    assert.deepEqual(latestUpdate(fixture), [])
    assert.equal(latestActivityUpdate(fixture).activities[0].has_unread_result, false)
    assert.equal(latestActivityUpdate(fixture).activities[0].last_read_message_id, null)
    assert.equal(fixture.timers.pending.length, 1)
    fixture.controller.dispose()
  }
})

test('permanent automatic read refusals do not retry after repeated focus and refresh', async () => {
  for (const code of [400, 403, 404]) {
    const refusal = new Error(`automatic read refused with business code ${code}`)
    refusal.response = { status: 200, data: { code } }
    const readRequests = []
    const fixture = createFixture({
      fetchTasks: async () => [createTask(`auto-refused-${code}`, {
        completed_message_id: 176 + code,
        completed_status: 'failed'
      })],
      markRead: async (sessionId, messageId) => {
        readRequests.push([sessionId, messageId])
        throw refusal
      }
    })

    await fixture.controller.setIdentity(`auto-refused-user-${code}`)
    fixture.controller.setViewingSession(`auto-refused-${code}`)
    await flushMicrotasks()
    assert.deepEqual(readRequests, [[`auto-refused-${code}`, 176 + code]])
    assert.deepEqual(latestUpdate(fixture), [])
    assert.equal(latestActivityUpdate(fixture).activities[0].has_unread_result, false)
    assert.equal(latestActivityUpdate(fixture).activities[0].last_read_message_id, null)
    assert.deepEqual(fixture.errors, [refusal])

    fixture.controller.setViewingSession(null)
    for (let attempt = 0; attempt < 3; attempt += 1) {
      fixture.controller.setViewingSession(`auto-refused-${code}`)
      await fixture.controller.refresh()
      fixture.controller.setViewingSession(null)
    }
    assert.deepEqual(readRequests, [[`auto-refused-${code}`, 176 + code]])
    assert.deepEqual(latestUpdate(fixture), [])
    assert.equal(latestActivityUpdate(fixture).activities[0].has_unread_result, false)
    fixture.controller.dispose()
  }
})
