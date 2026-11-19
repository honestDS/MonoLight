import assert from 'node:assert/strict'
import test from 'node:test'

import { mergeSessionActivities } from '../../../src/composables/chat/sessionActivity.js'
import {
  createControllerHarness,
  createDeferred,
  flushMicrotasks,
  makeActivity,
  makeSession
} from '../../support/chat/sessionActivityHarness.js'

test('mergeSessionActivities applies only the activity whitelist without mutating complete sessions', () => {
  const sessions = [
    makeSession('a', { custom: 'keep-a', is_loading: false }),
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
      is_loading: true,
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
      is_reply_running: true,
      is_loading: true
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

test('mergeSessionActivities sorts merged sessions by descending last_active without mutating inputs', () => {
  const sessions = [
    makeSession('a', { last_active: '2026-10-08 10:00:00' }),
    makeSession('b', { last_active: '2026-10-08 09:00:00' })
  ]
  const originalSessions = JSON.parse(JSON.stringify(sessions))
  const activities = [
    makeActivity('a', { last_active: '2026-10-08 10:00:00' }),
    makeActivity('b', { last_active: '2026-10-08 11:00:00' })
  ]
  const originalActivities = JSON.parse(JSON.stringify(activities))

  const merged = mergeSessionActivities(sessions, activities)
  const mergedB = merged.find(session => session.session_id === 'b')

  assert.deepEqual(merged.map(session => session.session_id), ['b', 'a'])
  assert.equal(mergedB.last_active, '2026-10-08 11:00:00')
  assert.equal(mergedB.title, sessions[1].title)
  assert.deepEqual(mergedB.config, sessions[1].config)
  assert.deepEqual(mergedB.reply_works, sessions[1].reply_works)
  assert.equal(mergedB.uid, sessions[1].uid)
  assert.deepEqual(sessions, originalSessions)
  assert.deepEqual(activities, originalActivities)
})

test('controller activity merge sorts known sessions while preserving activity order and complete fields', async () => {
  const initialSessions = [
    makeSession('a', { source: 'http', last_active: '2026-10-08 10:00:00', is_reply_running: false }),
    makeSession('b', { source: 'http', last_active: '2026-10-08 09:00:00', is_reply_running: false })
  ]
  const originalInitial = JSON.parse(JSON.stringify(initialSessions))
  const activity = [
    makeActivity('a', { source: 'http', last_active: '2026-10-08 10:00:00', is_reply_running: false }),
    makeActivity('b', { source: 'http', last_active: '2026-10-08 11:00:00', is_reply_running: false })
  ]
  let fullCalls = 0
  const harness = createControllerHarness({
    initialSessions,
    currentSessionId: 'a',
    fetchSessions: async () => {
      fullCalls += 1
      return initialSessions
    },
    fetchActivity: async () => activity
  })

  const first = await harness.controller.refreshActivity()
  await flushMicrotasks()
  assert.deepEqual(first, activity)
  assert.deepEqual(harness.getSessions().map(session => session.session_id), ['b', 'a'])
  for (const initial of initialSessions) {
    const current = harness.getSessions().find(session => session.session_id === initial.session_id)
    assert.equal(current.title, initial.title)
    assert.deepEqual(current.config, initial.config)
    assert.deepEqual(current.reply_works, initial.reply_works)
    assert.equal(current.uid, initial.uid)
  }

  const second = await harness.controller.refreshActivity()
  await flushMicrotasks()
  assert.deepEqual(second, activity)
  assert.deepEqual(harness.getSessions().map(session => session.session_id), ['b', 'a'])
  assert.equal(fullCalls, 0)
  assert.deepEqual(initialSessions, originalInitial)
  harness.controller.dispose()
})

test('mergeSessionActivities keeps stable order for equal, null, and invalid last_active values', () => {
  const sessions = [
    makeSession('missing', { last_active: null }),
    makeSession('invalid', { last_active: 'not-a-date' }),
    makeSession('a', { last_active: '2026-10-08 10:00:00' }),
    makeSession('b', { last_active: '2026-10-08 10:00:00' }),
    makeSession('c', { last_active: '2026-10-08 11:00:00' })
  ]
  const originalSessions = JSON.parse(JSON.stringify(sessions))
  const activity = [{ session_id: 'a', latest_message_id: 2 }]
  const originalActivity = JSON.parse(JSON.stringify(activity))

  const merged = mergeSessionActivities(sessions, activity)
  const repeated = mergeSessionActivities(merged, activity)

  assert.deepEqual(merged.map(session => session.session_id), ['c', 'a', 'b', 'missing', 'invalid'])
  assert.equal(merged.find(session => session.session_id === 'a').last_active, '2026-10-08 10:00:00')
  assert.deepEqual(repeated, merged)
  assert.deepEqual(sessions, originalSessions)
  assert.deepEqual(activity, originalActivity)
})

test('a newer activity overlays a stale in-flight full response', async () => {
  const fullResponse = createDeferred()
  const activityResponse = createDeferred()
  const harness = createControllerHarness({
    initialSessions: [makeSession('session-a', { title: 'initial', is_loading: false })],
    fetchSessions: () => fullResponse.promise,
    fetchActivity: () => activityResponse.promise
  })

  const full = harness.controller.refreshSessions()
  const activity = harness.controller.refreshActivity()
  const newerActivity = makeActivity('session-a', {
    latest_message_id: 20,
    last_active: '2026-10-08 00:20:00',
    source: 'local',
    is_reply_running: true,
    is_loading: true
  })
  activityResponse.resolve([newerActivity])
  assert.deepEqual(await activity, [newerActivity])

  fullResponse.resolve([makeSession('session-a', {
    title: 'stale full title',
    latest_message_id: 10,
    last_active: '2026-10-08 00:10:00',
    source: 'local',
    is_reply_running: false,
    is_loading: false
  })])
  const result = await full
  assert.equal(result[0].title, 'stale full title')
  assert.equal(result[0].latest_message_id, 20)
  assert.equal(result[0].last_active, '2026-10-08 00:20:00')
  assert.equal(result[0].is_reply_running, true)
  assert.equal(result[0].is_loading, true)
})

test('activity is_loading updates every session without refreshing non-current reply states', async () => {
  const initialSessions = [
    makeSession('own', { source: 'http', is_loading: false, is_reply_running: false }),
    makeSession('other', { source: 'http', is_loading: false, is_reply_running: false }),
    makeSession('summary', { source: 'external', is_loading: false, is_reply_running: false })
  ]
  const firstActivity = [
    makeActivity('own', { source: 'http', is_loading: false, is_reply_running: false }),
    makeActivity('other', { source: 'http', is_loading: true, is_reply_running: true }),
    makeActivity('summary', { source: 'external', is_loading: true, is_reply_running: false })
  ]
  const finalActivity = [
    makeActivity('own', { source: 'http', is_loading: false, is_reply_running: false }),
    makeActivity('other', { source: 'http', is_loading: false, is_reply_running: false }),
    makeActivity('summary', { source: 'external', is_loading: false, is_reply_running: false })
  ]
  const rounds = [firstActivity, firstActivity, finalActivity]
  let activityCalls = 0
  let fullCalls = 0
  const harness = createControllerHarness({
    initialSessions,
    currentSessionId: 'own',
    fetchSessions: async () => {
      fullCalls += 1
      return initialSessions
    },
    fetchActivity: async () => rounds[activityCalls++]
  })

  for (const round of rounds) {
    const returned = await harness.controller.refreshActivity()
    assert.deepEqual(returned, round)
    assert.deepEqual(harness.activitiesUpdated[harness.activitiesUpdated.length - 1], round)

    for (const initial of initialSessions) {
      const current = harness.getSessions().find(session => session.session_id === initial.session_id)
      const activity = round.find(item => item.session_id === initial.session_id)
      assert.equal(current.is_loading, activity.is_loading)
      assert.equal(current.title, initial.title)
      assert.deepEqual(current.config, initial.config)
      assert.deepEqual(current.reply_works, initial.reply_works)
      assert.equal(current.uid, initial.uid)
    }

    assert.equal(harness.getSessions().find(session => session.session_id === 'summary').is_reply_running, false)
    assert.equal(
      harness.getSessions().find(session => session.session_id === 'other').is_reply_running,
      round.find(item => item.session_id === 'other').is_reply_running
    )
  }

  assert.equal(fullCalls, 0)
  harness.controller.dispose()
})

test('a newer activity snapshot keeps sorted order over an in-flight full response', async () => {
  const fullResponse = createDeferred()
  const initialSessions = [
    makeSession('a', { last_active: '2026-10-08 10:00:00' }),
    makeSession('b', { last_active: '2026-10-08 09:00:00' })
  ]
  const activity = [
    makeActivity('a', { last_active: '2026-10-08 10:00:00' }),
    makeActivity('b', { last_active: '2026-10-08 11:00:00' })
  ]
  let fullCalls = 0
  const harness = createControllerHarness({
    initialSessions,
    fetchSessions: () => {
      fullCalls += 1
      return fullResponse.promise
    },
    fetchActivity: async () => activity
  })

  const full = harness.controller.refreshSessions()
  assert.deepEqual(await harness.controller.refreshActivity(), activity)
  assert.deepEqual(harness.getSessions().map(session => session.session_id), ['b', 'a'])

  fullResponse.resolve([
    makeSession('a', { title: 'Full A', last_active: '2026-10-08 10:00:00' }),
    makeSession('b', { title: 'Full B', last_active: '2026-10-08 09:00:00' })
  ])
  const result = await full

  assert.deepEqual(result.map(session => session.session_id), ['b', 'a'])
  assert.equal(result[0].last_active, '2026-10-08 11:00:00')
  assert.equal(result[0].title, 'Full B')
  assert.equal(result[1].title, 'Full A')
  assert.deepEqual(harness.getSessions().map(session => session.session_id), ['b', 'a'])
  assert.equal(harness.getSessions()[0].last_active, '2026-10-08 11:00:00')
  assert.equal(fullCalls, 1)
  harness.controller.dispose()
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
