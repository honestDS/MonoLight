import assert from 'node:assert/strict'
import test from 'node:test'

import { createSessionSubagentBoard } from '../../../src/composables/chat/sessionSubagentBoard.js'
import {
  assertScopeCleared,
  createApi,
  createDeferred,
  detailResponse,
  flushMicrotasks,
  listResponse,
  makeEntry
} from './sessionSubagentBoardHarness.js'

test('does not fetch without a session or while disabled, observes activity revisions, and preserves server active order', async t => {
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
  t.after(() => board.dispose())

  await board.refresh()
  assert.equal(board.observeActivity('session-a', 0), false)
  board.setScope('session-a', 'profile-a', 0)
  board.observeActivity('session-a', 0)
  assert.equal(harness.listCalls.length, 0)

  const firstList = createDeferred()
  harness.queueList(firstList.promise)
  board.setEnabled(true)
  assert.equal(harness.listCalls.length, 1)
  assert.deepEqual(harness.listCalls[0].params, { recent_limit: 10, active_page: 1 })
  board.observeActivity('session-a', 0)
  await flushMicrotasks()
  assert.equal(harness.listCalls.length, 1)

  const activeTwenty = makeEntry('active-twenty', 20)
  const activeTen = makeEntry('active-ten', 10)
  const recentSecond = makeEntry('recent-second', 30, { completed: true })
  const recentFirst = makeEntry('recent-first', 40, { completed: true })
  const secondList = createDeferred()
  harness.queueList(secondList.promise)
  board.observeActivity('session-a', 1)
  board.observeActivity('session-a', 2)
  board.observeActivity('session-a', 3)
  await flushMicrotasks()
  assert.equal(harness.listCalls.length, 1)
  assert.equal(harness.getMaxActiveListRequests(), 1)

  firstList.resolve(listResponse({
    active: [activeTwenty, activeTen],
    recent: [recentSecond, recentFirst],
    has_more_recent: true
  }))
  await flushMicrotasks()

  assert.deepEqual(board.active.value.map(entry => entry.work_id), [20, 10])
  assert.deepEqual(board.recent.value.map(entry => entry.subagent_id), [
    'recent-second',
    'recent-first'
  ])
  assert.equal(board.hasMoreRecent.value, true)
  assert.equal(board.loading.value, true)
  assert.equal(harness.listCalls.length, 2)
  assert.equal(harness.getMaxActiveListRequests(), 1)

  secondList.resolve(listResponse({
    active: [activeTwenty, activeTen],
    recent: [recentSecond, recentFirst],
    has_more_recent: true
  }))
  await flushMicrotasks()
  assert.equal(board.loading.value, false)
  assert.equal(harness.getMaxActiveListRequests(), 1)
  assert.equal(harness.listCalls.length, 2)

  board.observeActivity('session-a', 3)
  board.observeActivity('session-a', 2)
  board.observeActivity('other-session', 4)
  board.observeActivity('session-a', -1)
  board.observeActivity('session-a', 1.5)
  await flushMicrotasks()
  assert.equal(harness.listCalls.length, 2)
})

test('disabling aborts in-flight work, ignores background updates, reopening recovers, and dispose blocks later mutations', async t => {
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
  t.after(() => board.dispose())

  const keptEntry = makeEntry('kept', 10)
  harness.queueList(Promise.resolve(listResponse({ active: [keptEntry] })))
  board.setScope('session-a', 'profile-a', 0, 1)
  board.setEnabled(true)
  await flushMicrotasks()
  assert.deepEqual(board.active.value, [keptEntry])

  board.setEnabled(false)
  board.observeActivity('session-a', 1)
  await flushMicrotasks()
  assert.equal(harness.listCalls.length, 1)

  const inFlight = createDeferred()
  harness.queueList(inFlight.promise)
  board.setEnabled(true)
  await flushMicrotasks()
  assert.equal(harness.listCalls.length, 2)
  const inFlightCall = harness.listCalls[1]
  board.setEnabled(false)
  assert.equal(inFlightCall.signal.aborted, true)

  inFlight.resolve(listResponse({
    active: [makeEntry('stale', 99)],
    recent: [makeEntry('stale-recent', 100, { completed: true })]
  }))
  await flushMicrotasks()
  assert.deepEqual(board.active.value, [keptEntry])
  assert.deepEqual(board.recent.value, [])

  harness.queueList(Promise.resolve(listResponse({
    active: [makeEntry('recovered', 11)]
  })))
  board.setEnabled(true)
  await flushMicrotasks()
  assert.deepEqual(board.active.value.map(entry => entry.subagent_id), ['recovered'])

  const callsBeforeDispose = harness.listCalls.length
  board.dispose()
  board.observeActivity('session-a', 2)
  board.setEnabled(true)
  board.setScope('session-b', 'profile-b')
  await board.refresh()
  assert.equal(harness.listCalls.length, callsBeforeDispose)
  assert.deepEqual(board.active.value.map(entry => entry.subagent_id), ['recovered'])
})

test('scope changes clear state immediately and ignore stale list/detail success and failure paths', async t => {
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
  t.after(() => board.dispose())

  const listA = createDeferred()
  harness.queueList(listA.promise)
  board.setScope('session-a', 'profile-a')
  board.setEnabled(true)
  assert.equal(harness.listCalls.length, 1)

  const listB = createDeferred()
  harness.queueList(listB.promise)
  board.setScope('session-b', 'profile-b')
  assertScopeCleared(board)
  assert.equal(harness.listCalls[0].signal.aborted, true)
  assert.equal(harness.listCalls.length, 2)
  assert.equal(harness.listCalls[1].sessionId, 'session-b')

  listB.resolve(listResponse({
    profile_id: 2,
    active: [makeEntry('agent-b', 20)]
  }))
  await flushMicrotasks()
  listA.resolve(listResponse({
    profile_id: 1,
    active: [makeEntry('agent-a', 10)]
  }))
  await flushMicrotasks()
  assert.deepEqual(board.active.value.map(entry => entry.subagent_id), ['agent-b'])

  const detailB = createDeferred()
  harness.queueDetail(detailB.promise)
  const selectB = board.selectSubagent('agent-b')
  await flushMicrotasks()
  assert.equal(harness.detailCalls.length, 1)

  const listC = createDeferred()
  harness.queueList(listC.promise)
  board.setScope('session-b', 'profile-c')
  assertScopeCleared(board)
  assert.equal(harness.detailCalls[0].signal.aborted, true)
  detailB.reject(new Error('stale detail failure'))
  listC.resolve(listResponse({
    profile_id: 3,
    active: [makeEntry('agent-c', 30)]
  }))
  await Promise.all([selectB, flushMicrotasks()])
  assert.deepEqual(board.active.value.map(entry => entry.subagent_id), ['agent-c'])
  assert.equal(board.detailFailed.value, false)

  const detailC = createDeferred()
  harness.queueDetail(detailC.promise)
  const selectC = board.selectSubagent('agent-c')
  await flushMicrotasks()

  const listD = createDeferred()
  harness.queueList(listD.promise)
  board.setScope('session-d', 'profile-d')
  assertScopeCleared(board)
  assert.equal(harness.detailCalls[1].signal.aborted, true)
  detailC.resolve(detailResponse({ subagent_id: 'agent-c', work_id: 30 }))
  listD.resolve(listResponse({
    profile_id: 4,
    active: [makeEntry('agent-d', 40)]
  }))
  await Promise.all([selectC, flushMicrotasks()])
  assert.deepEqual(board.active.value.map(entry => entry.subagent_id), ['agent-d'])
  assert.equal(board.detail.value, null)
})

test('a server profile change clears the selected agent and invalidates its detail request', async t => {
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
  t.after(() => board.dispose())

  const entry = makeEntry('agent-a', 10)
  harness.queueList(Promise.resolve(listResponse({ profile_id: 1, active: [entry] })))
  board.setScope('session-a', 'profile-a', 0, 1)
  board.setEnabled(true)
  await flushMicrotasks()

  const detail = createDeferred()
  harness.queueDetail(detail.promise)
  const selection = board.selectSubagent('agent-a')
  await flushMicrotasks()
  assert.equal(board.selectedId.value, 'agent-a')

  harness.queueList(Promise.resolve(listResponse({
    profile_id: 2,
    active: [makeEntry('agent-a', 11)]
  })))
  board.observeActivity('session-a', 0, 2)
  await flushMicrotasks()
  assert.equal(harness.detailCalls[0].signal.aborted, true)
  assert.equal(board.selectedId.value, null)
  assert.equal(board.selectedWorkId.value, null)
  assert.equal(board.detail.value, null)
  assert.equal(board.nextBeforePostId.value, null)

  detail.resolve(detailResponse({ subagent_id: 'agent-a', work_id: 10 }))
  await selection
  await flushMicrotasks()
  assert.equal(board.detail.value, null)
  assert.equal(board.detailFailed.value, false)
})

test('failed list requests preserve data, recover on the next fixed revision snapshot, and do not recheck after success', async t => {
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
  t.after(() => board.dispose())

  const initialEntry = makeEntry('initial', 10)
  harness.queueList(Promise.resolve(listResponse({ active: [initialEntry] })))
  board.setScope('session-a', 'profile-a', 0, 1)
  board.setEnabled(true)
  await flushMicrotasks()
  assert.deepEqual(board.active.value, [initialEntry])

  const automaticFailure = new Error('automatic failure')
  harness.queueList(Promise.reject(automaticFailure))
  board.observeActivity('session-a', 1)
  await flushMicrotasks()
  assert.equal(board.failed.value, true)
  assert.deepEqual(board.active.value, [initialEntry])

  const manualRetry = createDeferred()
  harness.queueList(manualRetry.promise)
  board.observeActivity('session-a', 1)
  await flushMicrotasks()
  assert.equal(board.loading.value, true)
  assert.equal(board.failed.value, true)
  assert.deepEqual(board.active.value, [initialEntry])

  const recoveredEntry = makeEntry('recovered', 20)
  manualRetry.resolve(listResponse({ active: [recoveredEntry] }))
  await flushMicrotasks()
  assert.equal(board.failed.value, false)
  assert.deepEqual(board.active.value, [recoveredEntry])
  const callsAfterRecovery = harness.listCalls.length
  board.observeActivity('session-a', 1)
  await flushMicrotasks()
  assert.equal(harness.listCalls.length, callsAfterRecovery)
})

test('rechecks an expired activity lease from a snapshot without scheduling a timer', async t => {
  let now = 1_000
  t.mock.method(Date, 'now', () => now)
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
  t.after(() => board.dispose())

  harness.queueList(Promise.resolve(listResponse({
    active: [makeEntry('agent-a', 10)],
    lease_recheck_in_ms: 100
  })))
  board.setScope('session-a', 'profile-a', 0, 1)
  board.setEnabled(true)
  await flushMicrotasks()
  assert.equal(harness.listCalls.length, 1)

  now = 1_099
  board.observeActivity('session-a', 0, 1)
  await flushMicrotasks()
  assert.equal(harness.listCalls.length, 1)

  harness.queueList(Promise.resolve(listResponse({ active: [makeEntry('agent-a', 10)] })))
  now = 1_100
  board.observeActivity('session-a', 0, 1)
  await flushMicrotasks()
  assert.equal(harness.listCalls.length, 2)
})

test('loadMoreRecent adds one follow-up after an in-flight list request', async t => {
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
  t.after(() => board.dispose())

  const firstList = createDeferred()
  const secondList = createDeferred()
  harness.queueList(firstList.promise)
  board.setScope('session-a', 'profile-a', 0, 1)
  board.setEnabled(true)
  await flushMicrotasks()

  harness.queueList(secondList.promise)
  await board.loadMoreRecent()
  assert.equal(board.recentLimit.value, 20)
  assert.equal(harness.listCalls.length, 1)

  firstList.resolve(listResponse({ active: [makeEntry('agent-a', 10)] }))
  await flushMicrotasks()
  assert.equal(harness.listCalls.length, 2)
  assert.equal(harness.listCalls[1].params.recent_limit, 20)
  assert.equal(harness.getMaxActiveListRequests(), 1)

  secondList.resolve(listResponse({ active: [makeEntry('agent-a', 10)] }))
  await flushMicrotasks()
  assert.equal(harness.listCalls.length, 2)
  assert.equal(board.loading.value, false)
})

test('load more increases the recent limit by ten up to one hundred, stays capped, and resets per scope', async t => {
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
  t.after(() => board.dispose())

  board.setScope('session-a', 'profile-a')
  board.setEnabled(true)
  await flushMicrotasks()
  assert.equal(board.recentLimit.value, 10)
  assert.equal(harness.listCalls[0].params.recent_limit, 10)

  for (let expectedLimit = 20; expectedLimit <= 100; expectedLimit += 10) {
    await board.loadMoreRecent()
    assert.equal(board.recentLimit.value, expectedLimit)
    assert.equal(harness.listCalls[harness.listCalls.length - 1].params.recent_limit, expectedLimit)
  }

  for (let attempt = 0; attempt < 3; attempt += 1) {
    await board.loadMoreRecent()
    assert.equal(board.recentLimit.value, 100)
    assert.equal(harness.listCalls[harness.listCalls.length - 1].params.recent_limit, 100)
  }

  board.setScope('session-b', 'profile-b')
  await flushMicrotasks()
  assert.equal(board.recentLimit.value, 10)
  assert.equal(harness.listCalls[harness.listCalls.length - 1].sessionId, 'session-b')
  assert.equal(harness.listCalls[harness.listCalls.length - 1].params.recent_limit, 10)
})
