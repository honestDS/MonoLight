import assert from 'node:assert/strict'
import test from 'node:test'

import {
  clearAllContextSummaryWorks,
  clearContextSummaryRequest,
  createContextSummaryTracker,
  endContextSummaryWork,
  shouldIgnoreExternalSessionEvent,
  startContextSummaryWork
} from '../src/composables/chat/contextSummaryTracker.js'

test('keeps independent summary work active when another work ends', () => {
  const activeKeys = new Set()
  const requestKeys = new Map()

  startContextSummaryWork(activeKeys, requestKeys, { work_id: 11 }, 'request-a')
  startContextSummaryWork(activeKeys, requestKeys, { work_id: 12 }, 'request-b')
  endContextSummaryWork(activeKeys, requestKeys, { work_id: 11 }, 'request-a')

  assert.deepEqual([...activeKeys], ['work:12'])
  clearContextSummaryRequest(activeKeys, requestKeys, 'request-b')
  assert.equal(activeKeys.size, 0)
})

test('uses request fallback and clears only that request', () => {
  const activeKeys = new Set()
  const requestKeys = new Map()

  startContextSummaryWork(activeKeys, requestKeys, {}, 'request-a')
  startContextSummaryWork(activeKeys, requestKeys, {}, 'request-b')
  clearContextSummaryRequest(activeKeys, requestKeys, 'request-a')

  assert.deepEqual([...activeKeys], ['request:request-b'])
  clearAllContextSummaryWorks(activeKeys, requestKeys)
  assert.equal(activeKeys.size, 0)
  assert.equal(requestKeys.size, 0)
})

test('tracks session-scoped and work-scoped event sequences separately', () => {
  const sequenceBySession = new Map()

  assert.equal(shouldIgnoreExternalSessionEvent(sequenceBySession, { session_id: 's1', event_sequence_no: 8 }, 's1'), false)
  assert.equal(shouldIgnoreExternalSessionEvent(sequenceBySession, { session_id: 's1', event_sequence_no: 7 }, 's1'), true)
  assert.equal(shouldIgnoreExternalSessionEvent(sequenceBySession, { session_id: 's1', event_sequence_no: 8 }, 's1'), true)
  assert.equal(shouldIgnoreExternalSessionEvent(sequenceBySession, { session_id: 's1', work_id: 3, event_sequence_no: 1 }, 's1'), false)
  assert.equal(shouldIgnoreExternalSessionEvent(sequenceBySession, { session_id: 's1', work_id: 4, event_sequence_no: 1 }, 's1'), false)
  assert.equal(shouldIgnoreExternalSessionEvent(sequenceBySession, { session_id: 's1', work_id: 3, event_sequence_no: 1 }, 's1'), true)
  assert.equal(shouldIgnoreExternalSessionEvent(sequenceBySession, { session_id: 'other', event_sequence_no: 9 }, 's1'), true)
  assert.equal(shouldIgnoreExternalSessionEvent(sequenceBySession, { session_id: 's1' }, 's1'), false)
})

test('summary tracker cannot revive ended work and keeps work scopes isolated', () => {
  const tracker = createContextSummaryTracker()
  const activeKeys = new Set()
  const requestKeys = new Map()
  const startA = { session_id: 's1', work_id: 'work-a', event_sequence_no: 1 }
  const startB = { session_id: 's1', work_id: 'work-b', event_sequence_no: 1 }
  const endA = { session_id: 's1', work_id: 'work-a', event_sequence_no: 2 }

  assert.equal(tracker.shouldIgnoreExternalSessionEvent(startA, 's1'), false)
  tracker.startContextSummaryWork(activeKeys, requestKeys, startA, 'request-a')
  assert.equal(tracker.shouldIgnoreExternalSessionEvent(startB, 's1'), false)
  tracker.startContextSummaryWork(activeKeys, requestKeys, startB, 'request-b')
  assert.equal(tracker.shouldIgnoreExternalSessionEvent(endA, 's1'), false)
  tracker.endContextSummaryWork(activeKeys, requestKeys, endA, 'request-a')

  assert.deepEqual([...activeKeys], ['work:work-b'])
  assert.equal(tracker.shouldIgnoreExternalSessionEvent(startA, 's1'), true)
  assert.equal(tracker.shouldIgnoreExternalSessionEvent(endA, 's1'), true)

  const lateNewStartA = { session_id: 's1', work_id: 'work-a', event_sequence_no: 3 }
  assert.equal(tracker.shouldIgnoreExternalSessionEvent(lateNewStartA, 's1'), false)
  tracker.startContextSummaryWork(activeKeys, requestKeys, lateNewStartA, 'request-a')
  assert.deepEqual([...activeKeys], ['work:work-b'])

  assert.equal(tracker.shouldIgnoreExternalSessionEvent({ session_id: 's2', work_id: 'work-c', event_sequence_no: 1 }, 's1'), true)
  assert.deepEqual([...activeKeys], ['work:work-b'])
})

test('clearAll removes summary lifecycle and sequence tracking state', () => {
  const tracker = createContextSummaryTracker()
  const activeKeys = new Set()
  const requestKeys = new Map()
  const start = { session_id: 's1', work_id: 'work-a', event_sequence_no: 1 }
  const end = { session_id: 's1', work_id: 'work-a', event_sequence_no: 2 }

  assert.equal(tracker.shouldIgnoreExternalSessionEvent(start, 's1'), false)
  tracker.startContextSummaryWork(activeKeys, requestKeys, start, 'request-a')
  assert.equal(tracker.shouldIgnoreExternalSessionEvent(end, 's1'), false)
  tracker.endContextSummaryWork(activeKeys, requestKeys, end, 'request-a')
  tracker.clearAllContextSummaryWorks(activeKeys, requestKeys)

  assert.equal(tracker.shouldIgnoreExternalSessionEvent(start, 's1'), false)
  tracker.startContextSummaryWork(activeKeys, requestKeys, start, 'request-a')
  assert.deepEqual([...activeKeys], ['work:work-a'])
})
