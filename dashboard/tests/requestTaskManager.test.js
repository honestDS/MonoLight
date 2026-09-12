import test from 'node:test'
import assert from 'node:assert/strict'

import { createAbortableTaskManager, createLatestRequestTracker } from '../src/utils/requestTaskManager.js'

test('abortable task manager enforces single flight and invalidates active requests', () => {
  const manager = createAbortableTaskManager()
  const first = manager.begin('managed-list')

  assert.ok(first)
  assert.equal(manager.begin('managed-list'), null)
  assert.equal(manager.isCurrent(first), true)

  manager.invalidate()

  assert.equal(first.signal.aborted, true)
  assert.equal(manager.isCurrent(first), false)
  assert.equal(manager.activeCount(), 0)
  assert.ok(manager.begin('managed-list'))
})

test('latest request tracker rejects stale responses after replacement or invalidation', () => {
  const tracker = createLatestRequestTracker()
  const first = tracker.begin()
  const second = tracker.begin()

  assert.equal(tracker.isCurrent(first), false)
  assert.equal(tracker.isCurrent(second), true)

  tracker.invalidate()

  assert.equal(tracker.isCurrent(second), false)
})
