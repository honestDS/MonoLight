import assert from 'node:assert/strict'
import test from 'node:test'

import {
  clearAllContextSummaryWorks,
  clearContextSummaryRequest,
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

test('ignores stale external events but accepts work-scoped stream events', () => {
  const sequenceBySession = new Map()

  assert.equal(shouldIgnoreExternalSessionEvent(sequenceBySession, { session_id: 's1', event_sequence_no: 8 }, 's1'), false)
  assert.equal(shouldIgnoreExternalSessionEvent(sequenceBySession, { session_id: 's1', event_sequence_no: 7 }, 's1'), true)
  assert.equal(shouldIgnoreExternalSessionEvent(sequenceBySession, { session_id: 's1', event_sequence_no: 8 }, 's1'), true)
  assert.equal(shouldIgnoreExternalSessionEvent(sequenceBySession, { session_id: 's1', work_id: 3, event_sequence_no: 1 }, 's1'), false)
  assert.equal(shouldIgnoreExternalSessionEvent(sequenceBySession, { session_id: 's1' }, 's1'), false)
})
