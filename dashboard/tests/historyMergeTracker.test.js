import assert from 'node:assert/strict'
import test from 'node:test'
import { createHistoryMergeTracker } from '../src/composables/chat/historyMergeTracker.js'

test('a single request remains valid', () => {
  const tracker = createHistoryMergeTracker()
  const requestId = tracker.begin()

  assert.equal(tracker.isLatest(requestId), true)
})

test('a later request invalidates an earlier request', () => {
  const tracker = createHistoryMergeTracker()
  const earlierRequestId = tracker.begin()
  tracker.begin()

  assert.equal(tracker.isLatest(earlierRequestId), false)
})

test('the latest request remains valid after multiple requests', () => {
  const tracker = createHistoryMergeTracker()
  tracker.begin()
  const latestRequestId = tracker.begin()

  assert.equal(tracker.isLatest(latestRequestId), true)
})

test('only the last of multiple overlapping requests remains valid', () => {
  const tracker = createHistoryMergeTracker()
  const firstRequestId = tracker.begin()
  const secondRequestId = tracker.begin()
  const thirdRequestId = tracker.begin()

  assert.equal(tracker.isLatest(firstRequestId), false)
  assert.equal(tracker.isLatest(secondRequestId), false)
  assert.equal(tracker.isLatest(thirdRequestId), true)
})

test('invalidate makes every outstanding request invalid', () => {
  const tracker = createHistoryMergeTracker()
  const firstRequestId = tracker.begin()
  const secondRequestId = tracker.begin()

  tracker.invalidate()

  assert.equal(tracker.isLatest(firstRequestId), false)
  assert.equal(tracker.isLatest(secondRequestId), false)
})

test('a request started after invalidation remains valid', () => {
  const tracker = createHistoryMergeTracker()
  const invalidatedRequestId = tracker.begin()
  tracker.invalidate()
  const currentRequestId = tracker.begin()

  assert.equal(tracker.isLatest(invalidatedRequestId), false)
  assert.equal(tracker.isLatest(currentRequestId), true)
})

test('repeated begin and invalidate calls never revalidate old tokens', () => {
  const tracker = createHistoryMergeTracker()
  const firstRequestId = tracker.begin()
  tracker.invalidate()
  const secondRequestId = tracker.begin()
  tracker.invalidate()
  const thirdRequestId = tracker.begin()
  tracker.invalidate()

  assert.equal(tracker.isLatest(firstRequestId), false)
  assert.equal(tracker.isLatest(secondRequestId), false)
  assert.equal(tracker.isLatest(thirdRequestId), false)
})

test('only the C/B/A completion order applies the newest C snapshot', () => {
  const tracker = createHistoryMergeTracker()
  const appliedSnapshots = []
  const requestA = tracker.begin()
  const requestB = tracker.begin()
  const requestC = tracker.begin()

  for (const [request, snapshot] of [
    [requestC, 'C'],
    [requestB, 'B'],
    [requestA, 'A']
  ]) {
    if (tracker.isLatest(request)) appliedSnapshots.push(snapshot)
  }

  assert.deepEqual(appliedSnapshots, ['C'])
})

test('an invalidated request remains invalid after returning to its former session', () => {
  const tracker = createHistoryMergeTracker()
  const oldSessionRequest = tracker.begin()

  tracker.invalidate()
  const otherSessionRequest = tracker.begin()
  tracker.invalidate()
  const returnedSessionRequest = tracker.begin()

  assert.equal(tracker.isLatest(oldSessionRequest), false)
  assert.equal(tracker.isLatest(otherSessionRequest), false)
  assert.equal(tracker.isLatest(returnedSessionRequest), true)
})

test('interleaved history synchronizations apply only their latest token', () => {
  const tracker = createHistoryMergeTracker()
  const appliedSnapshots = []
  const firstSync = tracker.begin()
  const secondSync = tracker.begin()

  if (tracker.isLatest(firstSync)) appliedSnapshots.push('first')
  const thirdSync = tracker.begin()
  if (tracker.isLatest(secondSync)) appliedSnapshots.push('second')
  if (tracker.isLatest(thirdSync)) appliedSnapshots.push('third')

  assert.deepEqual(appliedSnapshots, ['third'])
})
