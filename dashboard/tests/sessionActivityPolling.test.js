import assert from 'node:assert/strict'
import test from 'node:test'

import {
  createSessionActivityController,
  mergeSessionActivities
} from '../src/composables/chat/sessionActivity.js'
import { createSessionListPoller } from '../src/composables/chat/sessionListLoading.js'

const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const flushMicrotasks = async (turns = 6) => {
  for (let index = 0; index < turns; index += 1) {
    await Promise.resolve()
  }
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

const makeSession = (sessionId, overrides = {}) => ({
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

const makeActivity = (sessionId, overrides = {}) => ({
  session_id: sessionId,
  latest_message_id: 2,
  last_active: '2026-10-08 00:01:00',
  source: 'local',
  is_reply_running: false,
  ...overrides
})

const createControllerHarness = ({
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

test('mergeSessionActivities applies only the activity whitelist without mutating complete sessions', () => {
  const sessions = [
    makeSession('a', { custom: 'keep-a' }),
    makeSession('b', { custom: 'keep-b' })
  ]
  const original = JSON.parse(JSON.stringify(sessions))
  const activities = [
    {
      session_id: 'a',
      title: 'must not replace the title',
      config: { model: 'must-not-replace-config' },
      reply_works: [{ work_id: 'must-not-replace-work' }],
      uid: 'must-not-replace-uid',
      latest_message_id: 2,
      last_active: '2026-10-08 00:02:00',
      source: 'ws',
      is_reply_running: true,
      ignored: 'must not be copied'
    },
    { session_id: 'a', latest_message_id: 3 },
    null,
    [],
    { session_id: null, latest_message_id: 99 },
    { session_id: '', latest_message_id: 99 },
    { session_id: '   ', latest_message_id: 99 },
    undefined
  ]

  const merged = mergeSessionActivities(sessions, activities)

  assert.deepEqual(merged, [
    {
      ...sessions[0],
      latest_message_id: 3,
      last_active: '2026-10-08 00:02:00',
      source: 'ws',
      is_reply_running: true
    },
    sessions[1]
  ])
  assert.notStrictEqual(merged[0], sessions[0])
  assert.strictEqual(merged[1], sessions[1])
  assert.equal(merged[0].title, sessions[0].title)
  assert.deepEqual(merged[0].config, sessions[0].config)
  assert.deepEqual(merged[0].reply_works, sessions[0].reply_works)
  assert.equal(merged[0].uid, sessions[0].uid)
  assert.deepEqual(sessions, original)
  assert.strictEqual(mergeSessionActivities(sessions, []), sessions)
  assert.strictEqual(mergeSessionActivities(sessions, null), sessions)
  assert.deepEqual(mergeSessionActivities(null, activities), [])
})

test('initial full refresh is followed by 1500ms activity-only polling, including empty lists', async () => {
  const fullSession = makeSession('known')
  const fullCalls = []
  const activityCalls = []
  const harness = createControllerHarness({
    fetchSessions: async () => {
      fullCalls.push('full')
      return [fullSession]
    },
    fetchActivity: async () => {
      activityCalls.push('activity')
      return []
    }
  })
  const timers = createManualTimerQueue()
  const poller = createSessionListPoller({
    refreshSessions: harness.controller.refreshActivity,
    schedule: timers.schedule,
    cancel: timers.cancel
  })

  assert.deepEqual(await harness.controller.refreshSessions(), [fullSession])
  assert.deepEqual(await poller.refreshNow(), [])
  assert.equal(fullCalls.length, 1)
  assert.equal(activityCalls.length, 1)
  assert.equal(timers.pending.length, 1)
  assert.equal(timers.pending[0].delay, 1500)

  const secondRound = await timers.tick()
  assert.deepEqual(secondRound, [])
  assert.equal(fullCalls.length, 1)
  assert.equal(activityCalls.length, 2)
  assert.equal(timers.pending.length, 1)
  assert.equal(timers.pending[0].delay, 1500)

  const thirdRound = await timers.tick()
  assert.deepEqual(thirdRound, [])
  assert.equal(fullCalls.length, 1)
  assert.equal(activityCalls.length, 3)
  assert.equal(timers.pending.length, 1)
  assert.equal(poller.isPolling(), true)

  poller.dispose()
  harness.controller.dispose()
})

test('a batch of unknown activity ids causes one full refresh and later activity keeps the recovered complete fields', async () => {
  const fullDeferred = createDeferred()
  const fullApplied = createDeferred()
  const fullCalls = []
  let activeFull = 0
  let maxActiveFull = 0
  let activityRound = 0
  const activityBatch = [
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
      return [{
        session_id: 'new-a',
        latest_message_id: 13,
        last_active: '2026-10-08 00:13:00',
        source: 'external',
        is_reply_running: false,
        ignored: 'still not returned'
      }]
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

  const laterActivity = [{
    session_id: 'new-a',
    latest_message_id: 13,
    last_active: '2026-10-08 00:13:00',
    source: 'external',
    is_reply_running: false,
    ignored: 'not returned'
  }]
  assert.deepEqual(await harness.controller.refreshActivity(), [
    {
      session_id: 'new-a',
      latest_message_id: 13,
      last_active: '2026-10-08 00:13:00',
      source: 'external',
      is_reply_running: false
    }
  ])
  assert.equal(fullCalls.length, 1)
  const recovered = harness.getSessions().find(session => session.session_id === 'new-a')
  assert.equal(recovered.title, 'Recovered A')
  assert.deepEqual(recovered.config, recoveredA.config)
  assert.deepEqual(recovered.reply_works, recoveredA.reply_works)
  assert.equal(recovered.uid, recoveredA.uid)
  assert.equal(recovered.latest_message_id, laterActivity[0].latest_message_id)
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
      return [makeActivity('missing', { source: 'external' })]
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
    fetchActivity: async () => [makeActivity('missing', { source: 'external' })],
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

test('selected Web reply changes trigger one full refresh per transition, not for stable or other sessions', async () => {
  const rounds = [
    [makeActivity('web', { source: 'http', is_reply_running: false }), makeActivity('other', { source: 'external', latest_message_id: 3 })],
    [makeActivity('web', { source: 'http', is_reply_running: false }), makeActivity('other', { source: 'external', latest_message_id: 4 })],
    [makeActivity('web', { source: 'http', is_reply_running: true }), makeActivity('other', { source: 'external', latest_message_id: 5 })],
    [makeActivity('web', { source: 'http', is_reply_running: true }), makeActivity('other', { source: 'external', latest_message_id: 6 })],
    [makeActivity('web', { source: 'http', is_reply_running: false }), makeActivity('other', { source: 'external', latest_message_id: 7 })],
    [makeActivity('web', { source: 'http', is_reply_running: false }), makeActivity('other', { source: 'external', latest_message_id: 8 })],
    [makeActivity('web', { source: 'http', is_reply_running: false }), makeActivity('other', { source: 'external', latest_message_id: 99 })]
  ]
  const fullCalls = []
  let activityCalls = 0
  const harness = createControllerHarness({
    initialSessions: [
      makeSession('web', { source: 'http' }),
      makeSession('other', { source: 'external' })
    ],
    currentSessionId: 'web',
    fetchSessions: async () => {
      fullCalls.push('full')
      return [
        makeSession('web', { source: 'http' }),
        makeSession('other', { source: 'external' })
      ]
    },
    fetchActivity: async () => rounds[activityCalls++]
  })

  const returned = []
  for (const round of rounds) {
    returned.push(await harness.controller.refreshActivity())
    await flushMicrotasks()
  }

  assert.deepEqual(returned, rounds)
  assert.equal(fullCalls.length, 2)
  assert.equal(activityCalls, rounds.length)
})

test('resetReplyObservation makes a subsequent first true Web observation refresh again', async () => {
  let fullCalls = 0
  const activity = makeActivity('web', { source: 'ws', is_reply_running: true })
  const harness = createControllerHarness({
    initialSessions: [makeSession('web', { source: 'ws' })],
    currentSessionId: 'web',
    fetchSessions: async () => {
      fullCalls += 1
      return [makeSession('web', { source: 'ws' })]
    },
    fetchActivity: async () => [activity]
  })

  assert.deepEqual(await harness.controller.refreshActivity(), [activity])
  await flushMicrotasks()
  assert.equal(fullCalls, 1)

  harness.controller.resetReplyObservation()
  assert.deepEqual(await harness.controller.refreshActivity(), [activity])
  await flushMicrotasks()
  assert.equal(fullCalls, 2)
})

test('unknown discovery and a Web state transition share one full refresh', async () => {
  let fullCalls = 0
  const activity = [
    makeActivity('web', { source: 'http', is_reply_running: true }),
    makeActivity('unknown', { source: 'external', is_reply_running: false })
  ]
  const harness = createControllerHarness({
    initialSessions: [makeSession('web', { source: 'http' })],
    currentSessionId: 'web',
    fetchSessions: async () => {
      fullCalls += 1
      return [makeSession('web', { source: 'http' }), makeSession('unknown', { source: 'external' })]
    },
    fetchActivity: async () => activity
  })

  assert.deepEqual(await harness.controller.refreshActivity(), activity)
  await flushMicrotasks()
  assert.equal(fullCalls, 1)
})

test('a Web observation during an in-flight full refresh creates one serial trailing full refresh', async () => {
  const firstFull = createDeferred()
  const secondFull = createDeferred()
  const events = []
  let fullCalls = 0
  let activeFull = 0
  let maxActiveFull = 0
  const secondStarted = createDeferred()
  const activity = makeActivity('web', { source: 'http', is_reply_running: true })
  const harness = createControllerHarness({
    initialSessions: [makeSession('web', { source: 'http' })],
    currentSessionId: 'web',
    fetchSessions: () => {
      fullCalls += 1
      events.push(`full-${fullCalls}`)
      activeFull += 1
      maxActiveFull = Math.max(maxActiveFull, activeFull)
      if (fullCalls === 2) secondStarted.resolve()
      const response = fullCalls === 1 ? firstFull : secondFull
      return response.promise.finally(() => {
        activeFull -= 1
      })
    },
    fetchActivity: async () => {
      events.push('activity')
      return [activity]
    }
  })

  const full = harness.controller.refreshSessions()
  const returnedActivity = await harness.controller.refreshActivity()
  assert.deepEqual(returnedActivity, [activity])
  assert.deepEqual(events, ['full-1', 'activity'])
  assert.equal(fullCalls, 1)
  assert.equal(maxActiveFull, 1)

  firstFull.resolve([makeSession('web', { source: 'http', title: 'first full' })])
  await secondStarted.promise
  assert.deepEqual(events, ['full-1', 'activity', 'full-2'])
  assert.equal(maxActiveFull, 1)

  secondFull.resolve([makeSession('web', { source: 'http', title: 'second full' })])
  await full
  assert.equal(fullCalls, 2)
  assert.equal(maxActiveFull, 1)
})

test('multiple manual full requests share one request and one trailing refresh', async () => {
  const firstFull = createDeferred()
  const secondFull = createDeferred()
  const secondStarted = createDeferred()
  let fullCalls = 0
  let activeFull = 0
  let maxActiveFull = 0
  const harness = createControllerHarness({
    fetchSessions: () => {
      fullCalls += 1
      activeFull += 1
      maxActiveFull = Math.max(maxActiveFull, activeFull)
      if (fullCalls === 2) secondStarted.resolve()
      const response = fullCalls === 1 ? firstFull : secondFull
      return response.promise.finally(() => {
        activeFull -= 1
      })
    }
  })

  const first = harness.controller.refreshSessions()
  const second = harness.controller.refreshSessions()
  const third = harness.controller.refreshSessions()
  assert.strictEqual(second, first)
  assert.strictEqual(third, first)
  assert.equal(fullCalls, 1)
  assert.equal(maxActiveFull, 1)

  firstFull.resolve([makeSession('first')])
  await secondStarted.promise
  assert.equal(fullCalls, 2)
  assert.equal(maxActiveFull, 1)
  secondFull.resolve([makeSession('second')])
  assert.deepEqual(await first, [makeSession('second')])
})

test('duplicate activity calls share one request and return normalized activity data', async () => {
  const firstActivity = createDeferred()
  let activityCalls = 0
  const harness = createControllerHarness({
    initialSessions: [makeSession('known')],
    fetchActivity: () => {
      activityCalls += 1
      return activityCalls === 1
        ? firstActivity.promise
        : Promise.resolve([makeActivity('known', { latest_message_id: 3 })])
    }
  })

  const first = harness.controller.refreshActivity()
  const duplicate = harness.controller.refreshActivity()
  assert.strictEqual(duplicate, first)
  assert.equal(activityCalls, 1)

  const rawActivity = {
    ...makeActivity('known'),
    ignored: 'not returned'
  }
  firstActivity.resolve([rawActivity])
  assert.deepEqual(await first, [makeActivity('known')])
  assert.deepEqual(await harness.controller.refreshActivity(), [makeActivity('known', { latest_message_id: 3 })])
  assert.equal(activityCalls, 2)
})

test('a newer activity overlays a stale in-flight full response', async () => {
  const fullResponse = createDeferred()
  const activityResponse = createDeferred()
  const harness = createControllerHarness({
    initialSessions: [makeSession('session-a', { title: 'initial' })],
    fetchSessions: () => fullResponse.promise,
    fetchActivity: () => activityResponse.promise
  })

  const full = harness.controller.refreshSessions()
  const activity = harness.controller.refreshActivity()
  const newerActivity = makeActivity('session-a', {
    latest_message_id: 20,
    last_active: '2026-10-08 00:20:00',
    source: 'local',
    is_reply_running: true
  })
  activityResponse.resolve([newerActivity])
  assert.deepEqual(await activity, [newerActivity])

  fullResponse.resolve([makeSession('session-a', {
    title: 'stale full title',
    latest_message_id: 10,
    last_active: '2026-10-08 00:10:00',
    source: 'local',
    is_reply_running: false
  })])
  const result = await full
  assert.equal(result[0].title, 'stale full title')
  assert.equal(result[0].latest_message_id, 20)
  assert.equal(result[0].last_active, '2026-10-08 00:20:00')
  assert.equal(result[0].is_reply_running, true)
})

test('an older activity response cannot overwrite a full response that started later', async () => {
  const activityResponse = createDeferred()
  const updated = makeSession('session-a', { title: 'new full title', latest_message_id: 30 })
  const harness = createControllerHarness({
    initialSessions: [makeSession('session-a', { title: 'initial' })],
    fetchSessions: async () => [updated],
    fetchActivity: () => activityResponse.promise
  })

  const staleActivity = harness.controller.refreshActivity()
  const full = harness.controller.refreshSessions()
  assert.deepEqual(await full, [updated])

  activityResponse.resolve([makeActivity('session-a', {
    latest_message_id: 1,
    last_active: '2026-10-08 00:01:00'
  })])
  assert.deepEqual(await staleActivity, [])
  assert.deepEqual(harness.getSessions(), [updated])
})

test('a failed full request keeps complete data and retries on the next manual refresh', async () => {
  const failure = new Error('full request failed')
  const initial = [makeSession('session-a', { title: 'original title' })]
  const updated = makeSession('session-a', { title: 'updated title' })
  let fullCalls = 0
  const harness = createControllerHarness({
    initialSessions: initial,
    fetchSessions: async () => {
      fullCalls += 1
      if (fullCalls === 1) throw failure
      return [updated]
    }
  })

  await assert.rejects(harness.controller.refreshSessions(), error => error === failure)
  assert.equal(fullCalls, 1)
  assert.equal(harness.writes.length, 0)
  assert.deepEqual(harness.getSessions(), initial)

  assert.deepEqual(await harness.controller.refreshSessions(), [updated])
  assert.equal(fullCalls, 2)
  assert.equal(harness.getSessions()[0].title, 'updated title')
})

test('a failed Web-triggered refresh preserves complete data and retries after the same state is observed', async () => {
  const failure = new Error('Web state refresh failed')
  const initial = makeSession('web', { source: 'http', title: 'complete title' })
  const recovered = makeSession('web', { source: 'http', title: 'recovered title' })
  const activity = makeActivity('web', { source: 'http', is_reply_running: true })
  let fullCalls = 0
  const harness = createControllerHarness({
    initialSessions: [initial],
    currentSessionId: 'web',
    fetchSessions: async () => {
      fullCalls += 1
      if (fullCalls === 1) throw failure
      return [recovered]
    },
    fetchActivity: async () => [activity]
  })

  assert.deepEqual(await harness.controller.refreshActivity(), [activity])
  await flushMicrotasks()
  assert.equal(fullCalls, 1)
  assert.deepEqual(harness.errors, [failure])
  assert.equal(harness.getSessions()[0].title, 'complete title')
  assert.deepEqual(harness.getSessions()[0].config, initial.config)
  assert.deepEqual(harness.getSessions()[0].reply_works, initial.reply_works)
  assert.equal(harness.getSessions()[0].uid, initial.uid)

  assert.deepEqual(await harness.controller.refreshActivity(), [activity])
  await flushMicrotasks()
  assert.equal(fullCalls, 2)
  assert.equal(harness.getSessions()[0].title, 'recovered title')
})

test('callback rejection reaches onError while a slow callback does not block activity polling or its timer', async () => {
  const callbackGate = createDeferred()
  const callbackFailure = new Error('activity callback failed')
  let callbackCalls = 0
  let activityCalls = 0
  const harness = createControllerHarness({
    initialSessions: [makeSession('known')],
    fetchActivity: async () => {
      activityCalls += 1
      return [makeActivity('known', { latest_message_id: activityCalls })]
    },
    onActivityUpdated: async () => {
      callbackCalls += 1
      if (callbackCalls !== 1) return undefined
      await callbackGate.promise
      throw callbackFailure
    }
  })
  const timers = createManualTimerQueue()
  const poller = createSessionListPoller({
    refreshSessions: harness.controller.refreshActivity,
    schedule: timers.schedule,
    cancel: timers.cancel
  })

  assert.deepEqual(await poller.refreshNow(), [makeActivity('known', { latest_message_id: 1 })])
  await flushMicrotasks()
  assert.equal(callbackCalls, 1)
  assert.equal(activityCalls, 1)
  assert.equal(timers.pending.length, 1)

  assert.deepEqual(await timers.tick(), [makeActivity('known', { latest_message_id: 2 })])
  await flushMicrotasks()
  assert.equal(callbackCalls, 2)
  assert.equal(activityCalls, 2)
  assert.equal(timers.pending.length, 1)
  assert.deepEqual(harness.errors, [])

  callbackGate.reject(callbackFailure)
  await flushMicrotasks()
  assert.deepEqual(harness.errors, [callbackFailure])

  poller.dispose()
  harness.controller.dispose()
})

test('dispose is idempotent and prevents late full, activity, and trailing mutations', async () => {
  const fullResponse = createDeferred()
  let fullCalls = 0
  const initial = [makeSession('keep')]
  const fullHarness = createControllerHarness({
    initialSessions: initial,
    fetchSessions: () => {
      fullCalls += 1
      return fullResponse.promise
    }
  })

  const full = fullHarness.controller.refreshSessions()
  const trailing = fullHarness.controller.refreshSessions()
  assert.strictEqual(trailing, full)
  assert.equal(fullCalls, 1)
  fullHarness.controller.dispose()
  fullHarness.controller.dispose()
  fullResponse.resolve([makeSession('late')])
  assert.deepEqual(await full, initial)
  assert.equal(fullCalls, 1)
  assert.deepEqual(fullHarness.writes, [])
  assert.deepEqual(fullHarness.sessionsUpdated, [])
  assert.deepEqual(await fullHarness.controller.refreshSessions(), initial)
  assert.deepEqual(await fullHarness.controller.refreshActivity(), [])

  const activityResponse = createDeferred()
  const activityHarness = createControllerHarness({
    initialSessions: [makeSession('keep')],
    fetchActivity: () => activityResponse.promise
  })
  const activity = activityHarness.controller.refreshActivity()
  activityHarness.controller.dispose()
  activityHarness.controller.dispose()
  activityResponse.resolve([makeActivity('keep', { latest_message_id: 99 })])
  assert.deepEqual(await activity, [])
  assert.deepEqual(activityHarness.writes, [])
  assert.deepEqual(activityHarness.activitiesUpdated, [])
})

test('onSessionsUpdated can start a second full refresh before the first promise cleanup', async () => {
  const responses = []
  const callbackSeen = createDeferred()
  let fetchCalls = 0
  let activeFull = 0
  let maxActiveFull = 0
  let callbackRefreshes = 0
  let secondRefresh
  let firstCallback = true
  let harness

  harness = createControllerHarness({
    initialSessions: [makeSession('session-a')],
    fetchSessions: () => {
      const call = ++fetchCalls
      const response = createDeferred()
      responses.push({ call, response })
      activeFull += 1
      maxActiveFull = Math.max(maxActiveFull, activeFull)
      return response.promise.finally(() => {
        activeFull -= 1
      })
    },
    onSessionsUpdated: () => {
      if (!firstCallback) return
      firstCallback = false
      callbackRefreshes += 1
      secondRefresh = harness.controller.refreshSessions()
      callbackSeen.resolve()
    }
  })

  const firstRefresh = harness.controller.refreshSessions()
  assert.equal(fetchCalls, 1)
  responses[0].response.resolve([makeSession('session-a', { title: `full-${responses[0].call}` })])

  const firstResult = await firstRefresh
  await callbackSeen.promise
  assert.equal(callbackRefreshes, 1)
  assert.equal(fetchCalls, 2)
  assert.ok(secondRefresh)

  responses[1].response.resolve([makeSession('session-a', { title: `full-${responses[1].call}` })])
  await secondRefresh

  assert.equal(firstResult[0].title, 'full-1')
  assert.equal(fetchCalls, 2)
  assert.equal(maxActiveFull, 1)
  assert.equal(harness.getSessions()[0].title, 'full-2')
  harness.controller.dispose()
})
