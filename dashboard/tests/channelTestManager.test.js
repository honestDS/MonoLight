import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createChannelTestManager } from '../src/utils/channelTestManager.js'

test('allows different keys to run concurrently with independent state', () => {
  const manager = createChannelTestManager()
  const first = manager.begin('first')
  const second = manager.begin('second')

  assert.notEqual(first, null)
  assert.notEqual(second, null)
  assert.notEqual(first.requestId, second.requestId)
  assert.equal(manager.isCurrent(first), true)
  assert.equal(manager.isCurrent(second), true)
  assert.equal(manager.activeCount(), 2)
})

test('rejects a duplicate begin for the same key', () => {
  const manager = createChannelTestManager()
  const first = manager.begin('same')

  assert.equal(manager.begin('same'), null)
  assert.equal(manager.isRunning('same'), true)
  assert.equal(manager.activeCount(), 1)
  assert.equal(manager.isCurrent(first), true)
})

test('finish only ends the corresponding task', () => {
  const manager = createChannelTestManager()
  const first = manager.begin('first')
  const second = manager.begin('second')

  assert.equal(manager.finish(first), true)
  assert.equal(manager.finish(first), false)
  assert.equal(manager.isCurrent(first), false)
  assert.equal(manager.isCurrent(second), true)
  assert.equal(manager.isRunning('first'), false)
  assert.equal(manager.isRunning('second'), true)
  assert.equal(manager.activeCount(), 1)
})

test('cancel aborts and invalidates the task', () => {
  const manager = createChannelTestManager()
  const token = manager.begin('cancelled')
  let currentDuringAbort
  token.signal.addEventListener('abort', () => {
    currentDuringAbort = manager.isCurrent(token)
  })

  assert.equal(manager.cancel('cancelled'), true)
  assert.equal(token.signal.aborted, true)
  assert.equal(currentDuringAbort, false)
  assert.equal(manager.isCurrent(token), false)
  assert.equal(manager.cancel('cancelled'), false)
  assert.equal(manager.activeCount(), 0)
})

test('invalidate aborts all tasks and invalidates old tokens', () => {
  const manager = createChannelTestManager()
  const first = manager.begin('first')
  const second = manager.begin('second')
  const currentDuringAbort = []
  first.signal.addEventListener('abort', () => currentDuringAbort.push(manager.isCurrent(first)))
  second.signal.addEventListener('abort', () => currentDuringAbort.push(manager.isCurrent(second)))

  manager.invalidate()

  assert.deepEqual(currentDuringAbort, [false, false])
  assert.equal(first.signal.aborted, true)
  assert.equal(second.signal.aborted, true)
  assert.equal(manager.isCurrent(first), false)
  assert.equal(manager.isCurrent(second), false)
  assert.equal(manager.activeCount(), 0)
})

test('new tasks are valid after invalidation', () => {
  const manager = createChannelTestManager()
  const oldToken = manager.begin('channel')

  manager.invalidate()
  const newToken = manager.begin('channel')

  assert.notEqual(newToken, null)
  assert.notEqual(newToken.generation, oldToken.generation)
  assert.equal(manager.isCurrent(oldToken), false)
  assert.equal(manager.isCurrent(newToken), true)
})

test('a late finish from an old task does not affect a new task', () => {
  const manager = createChannelTestManager()
  const oldToken = manager.begin('channel')

  manager.invalidate()
  const newToken = manager.begin('channel')

  assert.equal(manager.finish(oldToken), false)
  assert.equal(manager.isCurrent(newToken), true)
  assert.equal(manager.isRunning('channel'), true)
  assert.equal(manager.activeCount(), 1)
})
