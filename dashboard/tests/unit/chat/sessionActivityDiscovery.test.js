import assert from 'node:assert/strict'
import test from 'node:test'

import {
  createControllerHarness,
  createDeferred,
  flushMicrotasks,
  makeActivity,
  makeSession
} from '../../support/chat/sessionActivityHarness.js'

test('a batch of unknown activity ids causes one full refresh and later activity keeps the recovered complete fields', async () => {
  const fullDeferred = createDeferred()
  const fullApplied = createDeferred()
  const fullCalls = []
  let activeFull = 0
  let maxActiveFull = 0
  let activityRound = 0
  const activityBatch = [
    makeActivity('known'),
    {
      session_id: 'new-a',
      latest_message_id: 11,
      last_active: '2026-10-08 00:11:00',
      source: 'external',
      is_reply_running: false,
      ignored: 'not returned'
    },
    {
      session_id: 'new-b',
      latest_message_id: 12,
      last_active: '2026-10-08 00:12:00',
      source: 'external',
      is_reply_running: false
    }
  ]
  const laterActivity = [
    makeActivity('known'),
    {
      session_id: 'new-a',
      latest_message_id: 13,
      last_active: '2026-10-08 00:13:00',
      source: 'external',
      is_reply_running: false,
      ignored: 'still not returned'
    },
    {
      session_id: 'new-b',
      latest_message_id: 12,
      last_active: '2026-10-08 00:12:00',
      source: 'external',
      is_reply_running: false
    }
  ]
  const recoveredA = makeSession('new-a', { title: 'Recovered A' })
  const recoveredB = makeSession('new-b', { title: 'Recovered B' })
  const harness = createControllerHarness({
    initialSessions: [makeSession('known')],
    fetchSessions: () => {
      fullCalls.push('full')
      activeFull += 1
      maxActiveFull = Math.max(maxActiveFull, activeFull)
      return fullDeferred.promise.finally(() => {
        activeFull -= 1
      })
    },
    fetchActivity: async () => {
      activityRound += 1
      if (activityRound < 3) return activityBatch
      return laterActivity
    },
    onSessionsUpdated: value => fullApplied.resolve(value)
  })

  const expectedActivity = activityBatch.map(({ ignored, ...activity }) => activity)
  assert.deepEqual(await harness.controller.refreshActivity(), expectedActivity)
  assert.equal(fullCalls.length, 1)

  assert.deepEqual(await harness.controller.refreshActivity(), expectedActivity)
  assert.equal(fullCalls.length, 1)

  fullDeferred.resolve([makeSession('known'), recoveredA, recoveredB])
  await fullApplied.promise
  assert.equal(maxActiveFull, 1)
  assert.equal(harness.getSessions().find(session => session.session_id === 'new-a').title, 'Recovered A')
  assert.equal(harness.getSessions().find(session => session.session_id === 'new-b').title, 'Recovered B')

  assert.deepEqual(await harness.controller.refreshActivity(), laterActivity.map(({ ignored, ...activity }) => activity))
  assert.equal(fullCalls.length, 1)
  const recovered = harness.getSessions().find(session => session.session_id === 'new-a')
  assert.equal(recovered.title, 'Recovered A')
  assert.deepEqual(recovered.config, recoveredA.config)
  assert.deepEqual(recovered.reply_works, recoveredA.reply_works)
  assert.equal(recovered.uid, recoveredA.uid)
  assert.equal(recovered.latest_message_id, laterActivity.find(activity => activity.session_id === 'new-a').latest_message_id)
})

test('an unknown id omitted by a full response does not cause a full request on every activity round', async () => {
  const fullApplied = createDeferred()
  let fullCalls = 0
  let activityCalls = 0
  const harness = createControllerHarness({
    initialSessions: [makeSession('known')],
    fetchSessions: async () => {
      fullCalls += 1
      return [makeSession('known')]
    },
    fetchActivity: async () => {
      activityCalls += 1
      return [
        makeActivity('known', { latest_message_id: 1, last_active: '2026-10-08 00:00:00' }),
        makeActivity('missing', { source: 'external' })
      ]
    },
    onSessionsUpdated: value => fullApplied.resolve(value)
  })

  await harness.controller.refreshActivity()
  await fullApplied.promise
  await harness.controller.refreshActivity()
  await harness.controller.refreshActivity()

  assert.equal(fullCalls, 1)
  assert.equal(activityCalls, 3)
  assert.deepEqual(harness.getSessions(), [makeSession('known')])
})

test('failed unknown-session discovery clears its marker so the next observation retries', async () => {
  const failure = new Error('full discovery failed')
  const fullApplied = createDeferred()
  let fullCalls = 0
  const recovered = makeSession('missing', { title: 'Recovered after retry' })
  const harness = createControllerHarness({
    initialSessions: [makeSession('known')],
    fetchSessions: async () => {
      fullCalls += 1
      if (fullCalls === 1) throw failure
      return [makeSession('known'), recovered]
    },
    fetchActivity: async () => [
      makeActivity('known', { latest_message_id: 1, last_active: '2026-10-08 00:00:00' }),
      makeActivity('missing', { source: 'external' })
    ],
    onSessionsUpdated: value => fullApplied.resolve(value)
  })

  await harness.controller.refreshActivity()
  await flushMicrotasks()
  assert.equal(fullCalls, 1)
  assert.deepEqual(harness.errors, [failure])
  assert.deepEqual(harness.getSessions(), [makeSession('known')])

  await harness.controller.refreshActivity()
  await fullApplied.promise
  assert.equal(fullCalls, 2)
  assert.equal(harness.getSessions().find(session => session.session_id === 'missing').title, 'Recovered after retry')
})

test('a missing activity id is removed after full confirmation for selected and unselected sessions', async () => {
  for (const currentSessionId of ['deleted', 'kept']) {
    const initialKept = makeSession('kept', { title: 'Initial kept', custom: 'initial value' })
    const deleted = makeSession('deleted', { title: 'Deleted session' })
    const refreshedKept = makeSession('kept', {
      title: 'Refreshed kept',
      custom: 'complete value',
      latest_message_id: 2,
      last_active: '2026-10-08 00:01:00'
    })
    const activity = [makeActivity('kept')]
    const fullApplied = createDeferred()
    let fullCalls = 0
    const harness = createControllerHarness({
      initialSessions: [initialKept, deleted],
      currentSessionId,
      fetchSessions: async () => {
        fullCalls += 1
        return [refreshedKept]
      },
      fetchActivity: async () => activity,
      onSessionsUpdated: value => fullApplied.resolve(value)
    })

    assert.deepEqual(await harness.controller.refreshActivity(), activity)
    await flushMicrotasks()
    assert.equal(fullCalls, 1)
    await fullApplied.promise
    await flushMicrotasks()
    assert.deepEqual(harness.getSessions(), [refreshedKept])
    assert.equal(harness.getSessions()[0].title, 'Refreshed kept')
    assert.deepEqual(harness.getSessions()[0].config, refreshedKept.config)
    assert.deepEqual(harness.getSessions()[0].reply_works, refreshedKept.reply_works)
    assert.equal(harness.getSessions()[0].uid, refreshedKept.uid)
    assert.equal(fullCalls, 1)

    assert.deepEqual(await harness.controller.refreshActivity(), activity)
    await flushMicrotasks()
    assert.equal(fullCalls, 1)
    assert.deepEqual(harness.getSessions(), [refreshedKept])
    harness.controller.dispose()
  }
})

test('an empty activity snapshot removes the last session without repeating full confirmation', async () => {
  const fullApplied = createDeferred()
  let fullCalls = 0
  const harness = createControllerHarness({
    initialSessions: [makeSession('deleted')],
    fetchSessions: async () => {
      fullCalls += 1
      return []
    },
    fetchActivity: async () => [],
    onSessionsUpdated: value => fullApplied.resolve(value)
  })

  assert.deepEqual(await harness.controller.refreshActivity(), [])
  await flushMicrotasks()
  assert.equal(fullCalls, 1)
  await fullApplied.promise
  assert.deepEqual(harness.getSessions(), [])
  assert.equal(fullCalls, 1)

  assert.deepEqual(await harness.controller.refreshActivity(), [])
  await flushMicrotasks()
  assert.deepEqual(harness.getSessions(), [])
  assert.equal(fullCalls, 1)
  harness.controller.dispose()
})

test('a failed deletion confirmation preserves the session and retries on the same activity snapshot', async () => {
  const failure = new Error('deletion confirmation failed')
  const deleted = makeSession('deleted')
  const fullApplied = createDeferred()
  let fullCalls = 0
  const harness = createControllerHarness({
    initialSessions: [deleted],
    fetchSessions: async () => {
      fullCalls += 1
      if (fullCalls === 1) throw failure
      return []
    },
    fetchActivity: async () => [],
    onSessionsUpdated: value => fullApplied.resolve(value)
  })

  assert.deepEqual(await harness.controller.refreshActivity(), [])
  await flushMicrotasks()
  assert.equal(fullCalls, 1)
  assert.deepEqual(harness.getSessions(), [deleted])
  assert.deepEqual(harness.errors, [failure])

  assert.deepEqual(await harness.controller.refreshActivity(), [])
  await flushMicrotasks()
  assert.equal(fullCalls, 2)
  await fullApplied.promise
  assert.equal(fullCalls, 2)
  assert.deepEqual(harness.getSessions(), [])
  harness.controller.dispose()
})

test('a missing id during an in-flight full refresh queues one serial confirmation without resurrecting it', async () => {
  const firstFull = createDeferred()
  const secondFull = createDeferred()
  const secondStarted = createDeferred()
  const oldFullSessions = [
    makeSession('kept', { title: 'Old full kept' }),
    makeSession('deleted', { title: 'Old full deleted' })
  ]
  const newFullSessions = [makeSession('kept', {
    title: 'New full kept',
    latest_message_id: 2,
    last_active: '2026-10-08 00:01:00'
  })]
  let fullCalls = 0
  let activeFull = 0
  let maxActiveFull = 0
  const harness = createControllerHarness({
    initialSessions: [makeSession('kept'), makeSession('deleted')],
    fetchSessions: () => {
      fullCalls += 1
      activeFull += 1
      maxActiveFull = Math.max(maxActiveFull, activeFull)
      if (fullCalls === 2) secondStarted.resolve()
      const response = fullCalls === 1 ? firstFull : secondFull
      return response.promise.finally(() => {
        activeFull -= 1
      })
    },
    fetchActivity: async () => [makeActivity('kept')]
  })

  const full = harness.controller.refreshSessions()
  assert.equal(fullCalls, 1)
  assert.deepEqual(await harness.controller.refreshActivity(), [makeActivity('kept')])
  assert.equal(fullCalls, 1)

  firstFull.resolve(oldFullSessions)
  await flushMicrotasks()
  assert.equal(fullCalls, 2)
  await secondStarted.promise
  assert.equal(fullCalls, 2)
  assert.equal(maxActiveFull, 1)

  secondFull.resolve(newFullSessions)
  await full
  assert.equal(fullCalls, 2)
  assert.equal(maxActiveFull, 1)
  assert.deepEqual(harness.getSessions(), newFullSessions)
  assert.equal(harness.getSessions().some(session => session.session_id === 'deleted'), false)
  harness.controller.dispose()
})
