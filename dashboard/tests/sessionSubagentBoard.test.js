import assert from 'node:assert/strict'
import test from 'node:test'

import { createSessionSubagentBoard } from '../src/composables/chat/sessionSubagentBoard.js'

const createDeferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const flushMicrotasks = async (turns = 8) => {
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

const makeEntry = (subagentId, workId, overrides = {}) => ({
  name: `Agent ${subagentId}`,
  subagent_id: subagentId,
  work_id: workId,
  latest_post_id: null,
  status: 'running',
  completed: false,
  created_at: '2026-11-14T12:00:00Z',
  updated_at: '2026-11-14T12:01:00Z',
  stage: 'working',
  preview: `preview-${subagentId}`,
  ...overrides
})

const makePost = (id, workId, overrides = {}) => ({
  id,
  work_id: workId,
  stage: 'working',
  content: `post-${id}`,
  created_at: '2026-11-14T12:02:00Z',
  ...overrides
})

const makeRound = workId => ({
  work_id: workId,
  created_at: `2026-11-14T12:${String(workId).padStart(2, '0')}:00Z`
})

const makeDetail = (overrides = {}) => {
  const workId = overrides.work_id ?? 101
  return {
    subagent_id: 'agent-a',
    name: 'Agent agent-a',
    work_id: workId,
    status: 'running',
    completed: false,
    rounds: [{ work_id: workId, created_at: '2026-11-14T12:00:00Z' }],
    posts: [],
    next_before_round_id: null,
    next_before_post_id: null,
    result_text: null,
    reason: null,
    ended_without_reply: false,
    ...overrides
  }
}

const listResponse = ({
  profile_id: profileId = 1,
  active = [],
  recent = [],
  has_more_recent: hasMoreRecent = false,
  active_page: activePage = 1,
  active_page_size: activePageSize = 20,
  active_total: activeTotal = active.length,
  selected = null
} = {}) => ({
  data: {
    data: {
      profile_id: profileId,
      active_page: activePage,
      active_page_size: activePageSize,
      active_total: activeTotal,
      active,
      recent,
      has_more_recent: hasMoreRecent,
      selected
    }
  }
})

const detailResponse = overrides => ({
  data: {
    data: makeDetail(overrides)
  }
})

const postPageResponse = ({ work_id: workId, posts, next_before_post_id: nextBeforePostId }) => ({
  data: {
    data: {
      work_id: workId,
      posts,
      next_before_post_id: nextBeforePostId
    }
  }
})

const roundPageResponse = ({ rounds, next_before_round_id: nextBeforeRoundId }) => ({
  data: {
    data: {
      rounds,
      next_before_round_id: nextBeforeRoundId
    }
  }
})

const createApi = () => {
  const listQueue = []
  const detailQueue = []
  const listCalls = []
  const detailCalls = []
  let activeListRequests = 0
  let maxActiveListRequests = 0

  const take = (queue, fallback) => {
    const value = queue.length > 0 ? queue.shift() : fallback
    return value && typeof value.then === 'function' ? value : Promise.resolve(value)
  }

  const api = {
    sessionSubagents: (sessionId, params, config) => {
      const requestConfig = config || {}
      listCalls.push({
        sessionId,
        params: { ...params },
        signal: requestConfig.signal
      })
      activeListRequests += 1
      maxActiveListRequests = Math.max(maxActiveListRequests, activeListRequests)
      return take(listQueue, listResponse()).finally(() => {
        activeListRequests -= 1
      })
    },
    sessionSubagentDetail: (sessionId, subagentId, params, config) => {
      const requestConfig = config || {}
      detailCalls.push({
        sessionId,
        subagentId,
        params: { ...params },
        signal: requestConfig.signal
      })
      return take(detailQueue, detailResponse({ subagent_id: subagentId }))
    }
  }

  return {
    api,
    listCalls,
    detailCalls,
    queueList: value => listQueue.push(value),
    queueDetail: value => detailQueue.push(value),
    getMaxActiveListRequests: () => maxActiveListRequests
  }
}

const assertScopeCleared = board => {
  assert.deepEqual(board.active.value, [])
  assert.deepEqual(board.recent.value, [])
  assert.equal(board.hasMoreRecent.value, false)
  assert.equal(board.failed.value, false)
  assert.equal(board.detail.value, null)
  assert.equal(board.detailLoading.value, false)
  assert.equal(board.detailFailed.value, false)
  assert.equal(board.selectedId.value, null)
  assert.equal(board.selectedWorkId.value, null)
  assert.equal(board.nextBeforePostId.value, null)
}

test('does not fetch without a session or while disabled, polls once after each completion, and preserves server active order', async t => {
  const timers = createManualTimerQueue()
  const harness = createApi()
  const board = createSessionSubagentBoard({
    api: harness.api,
    schedule: timers.schedule,
    cancel: timers.cancel
  })
  t.after(() => board.dispose())

  await board.refresh()
  board.setScope('session-a', 'profile-a')
  assert.equal(harness.listCalls.length, 0)

  const firstList = createDeferred()
  harness.queueList(firstList.promise)
  board.setEnabled(true)
  assert.equal(harness.listCalls.length, 1)
  assert.deepEqual(harness.listCalls[0].params, { recent_limit: 10, active_page: 1 })

  const activeTwenty = makeEntry('active-twenty', 20)
  const activeTen = makeEntry('active-ten', 10)
  const recentSecond = makeEntry('recent-second', 30, { completed: true })
  const recentFirst = makeEntry('recent-first', 40, { completed: true })
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
  assert.equal(board.loading.value, false)
  assert.equal(timers.pending.length, 1)
  assert.equal(timers.pending[0].delay, 2000)

  const secondList = createDeferred()
  harness.queueList(secondList.promise)
  const secondTick = timers.tick()
  await flushMicrotasks()
  assert.equal(harness.listCalls.length, 2)
  await board.refresh()
  assert.equal(harness.listCalls.length, 2)
  assert.equal(harness.getMaxActiveListRequests(), 1)

  secondList.resolve(listResponse({ active: [activeTen, activeTwenty] }))
  await secondTick
  assert.equal(harness.getMaxActiveListRequests(), 1)
  assert.equal(timers.pending.length, 1)
  assert.equal(timers.pending[0].delay, 2000)
  assert.equal(timers.scheduled.every(handle => handle.delay === 2000), true)
})

test('disabling aborts in-flight work and cancels timers, reopening recovers, and dispose blocks later mutations', async t => {
  const timers = createManualTimerQueue()
  const harness = createApi()
  const board = createSessionSubagentBoard({
    api: harness.api,
    schedule: timers.schedule,
    cancel: timers.cancel
  })
  t.after(() => board.dispose())

  const keptEntry = makeEntry('kept', 10)
  harness.queueList(Promise.resolve(listResponse({ active: [keptEntry] })))
  board.setScope('session-a', 'profile-a')
  board.setEnabled(true)
  await flushMicrotasks()
  assert.deepEqual(board.active.value, [keptEntry])
  assert.equal(timers.pending.length, 1)

  const scheduledTimer = timers.pending[0]
  board.setEnabled(false)
  assert.equal(timers.pending.length, 0)
  assert.equal(timers.cancelled.includes(scheduledTimer), true)

  const inFlight = createDeferred()
  harness.queueList(inFlight.promise)
  board.setEnabled(true)
  await flushMicrotasks()
  assert.equal(harness.listCalls.length, 2)
  const inFlightCall = harness.listCalls[1]
  board.setEnabled(false)
  assert.equal(inFlightCall.signal.aborted, true)
  assert.equal(timers.pending.length, 0)

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
  assert.equal(timers.pending.length, 1)

  const callsBeforeDispose = harness.listCalls.length
  const lateTimer = timers.pending[0]
  board.dispose()
  assert.equal(timers.pending.length, 0)
  await lateTimer.callback()
  board.setEnabled(true)
  board.setScope('session-b', 'profile-b')
  await board.refresh()
  assert.equal(harness.listCalls.length, callsBeforeDispose)
  assert.deepEqual(board.active.value.map(entry => entry.subagent_id), ['recovered'])
})

test('scope changes clear state immediately and ignore stale list/detail success and failure paths', async t => {
  const timers = createManualTimerQueue()
  const harness = createApi()
  const board = createSessionSubagentBoard({
    api: harness.api,
    schedule: timers.schedule,
    cancel: timers.cancel
  })
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
  const timers = createManualTimerQueue()
  const harness = createApi()
  const board = createSessionSubagentBoard({
    api: harness.api,
    schedule: timers.schedule,
    cancel: timers.cancel
  })
  t.after(() => board.dispose())

  const entry = makeEntry('agent-a', 10)
  harness.queueList(Promise.resolve(listResponse({ profile_id: 1, active: [entry] })))
  board.setScope('session-a', 'profile-a')
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
  await board.refresh()
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

test('failed list requests preserve data, keep failed until success, and recover on manual and timer retries', async t => {
  const timers = createManualTimerQueue()
  const harness = createApi()
  const board = createSessionSubagentBoard({
    api: harness.api,
    schedule: timers.schedule,
    cancel: timers.cancel
  })
  t.after(() => board.dispose())

  const initialEntry = makeEntry('initial', 10)
  harness.queueList(Promise.resolve(listResponse({ active: [initialEntry] })))
  board.setScope('session-a', 'profile-a')
  board.setEnabled(true)
  await flushMicrotasks()
  assert.deepEqual(board.active.value, [initialEntry])

  const automaticFailure = new Error('automatic failure')
  harness.queueList(Promise.reject(automaticFailure))
  await timers.tick()
  assert.equal(board.failed.value, true)
  assert.deepEqual(board.active.value, [initialEntry])
  assert.equal(timers.pending.length, 1)

  const manualRetry = createDeferred()
  harness.queueList(manualRetry.promise)
  const retryRequest = board.refresh()
  await flushMicrotasks()
  assert.equal(board.loading.value, true)
  assert.equal(board.failed.value, true)
  assert.deepEqual(board.active.value, [initialEntry])
  assert.equal(timers.pending.length, 0)

  const recoveredEntry = makeEntry('recovered', 20)
  manualRetry.resolve(listResponse({ active: [recoveredEntry] }))
  await retryRequest
  assert.equal(board.failed.value, false)
  assert.deepEqual(board.active.value, [recoveredEntry])
  assert.equal(timers.pending.length, 1)

  const timerFailure = new Error('timer failure')
  harness.queueList(Promise.reject(timerFailure))
  await timers.tick()
  assert.equal(board.failed.value, true)
  assert.deepEqual(board.active.value, [recoveredEntry])
  assert.equal(timers.pending.length, 1)

  harness.queueList(Promise.resolve(listResponse({
    active: [makeEntry('timer-recovered', 30)]
  })))
  await timers.tick()
  assert.equal(board.failed.value, false)
  assert.deepEqual(board.active.value.map(entry => entry.subagent_id), ['timer-recovered'])
  assert.equal(timers.pending.length, 1)
})

test('selection and round changes abort stale detail requests, follow the latest round, and pass historical work ids', async t => {
  const timers = createManualTimerQueue()
  const harness = createApi()
  const board = createSessionSubagentBoard({
    api: harness.api,
    schedule: timers.schedule,
    cancel: timers.cancel
  })
  t.after(() => board.dispose())

  harness.queueList(Promise.resolve(listResponse({
    active: [makeEntry('agent-a', 20)]
  })))
  board.setScope('session-a', 'profile-a')
  board.setEnabled(true)
  await flushMicrotasks()

  const latest = createDeferred()
  harness.queueDetail(latest.promise)
  const latestRequest = board.selectSubagent('agent-a')
  await flushMicrotasks()
  assert.deepEqual(harness.detailCalls[0].params, {})

  const historical = createDeferred()
  harness.queueDetail(historical.promise)
  const historicalRequest = board.selectRound(10)
  await flushMicrotasks()
  assert.equal(harness.detailCalls[0].signal.aborted, true)
  assert.deepEqual(harness.detailCalls[1].params, { work_id: 10 })
  assert.equal(board.selectedWorkId.value, 10)

  latest.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 20,
    posts: [makePost(200, 20)]
  }))
  await latestRequest
  assert.equal(board.detail.value, null)

  historical.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 10,
    status: 'succeeded',
    completed: true,
    rounds: [
      { work_id: 10, created_at: '2026-11-14T12:00:00Z' },
      { work_id: 20, created_at: '2026-11-14T12:01:00Z' }
    ],
    posts: [makePost(100, 10)]
  }))
  await historicalRequest
  assert.equal(board.detail.value.work_id, 10)
  assert.deepEqual(board.detail.value.posts.map(post => post.id), [100])

  const staleRound = createDeferred()
  harness.queueDetail(staleRound.promise)
  const staleRequest = board.selectRound(11)
  await flushMicrotasks()

  const latestAgain = createDeferred()
  harness.queueDetail(latestAgain.promise)
  const latestAgainRequest = board.selectRound(null)
  await flushMicrotasks()
  assert.equal(harness.detailCalls[2].signal.aborted, true)
  assert.deepEqual(harness.detailCalls[3].params, {})
  assert.equal(board.selectedWorkId.value, null)

  staleRound.reject(new Error('stale round failure'))
  latestAgain.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 20,
    posts: [makePost(220, 20)]
  }))
  await Promise.all([staleRequest, latestAgainRequest])
  assert.equal(board.detail.value.work_id, 20)
  assert.deepEqual(board.detail.value.posts.map(post => post.id), [220])
  assert.equal(board.detailFailed.value, false)
})

test('a detail failure keeps the previous detail until a later refresh succeeds', async t => {
  const timers = createManualTimerQueue()
  const harness = createApi()
  const board = createSessionSubagentBoard({
    api: harness.api,
    schedule: timers.schedule,
    cancel: timers.cancel
  })
  t.after(() => board.dispose())

  const entry = makeEntry('agent-a', 20)
  harness.queueList(Promise.resolve(listResponse({ active: [entry] })))
  board.setScope('session-a', 'profile-a')
  board.setEnabled(true)
  await flushMicrotasks()

  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 20,
    status: 'running',
    completed: false,
    posts: [makePost(20, 20, { content: 'old detail' })],
    next_before_post_id: 10,
    result_text: 'old result',
    reason: null,
    ended_without_reply: false
  })))
  await board.selectSubagent('agent-a')
  assert.equal(board.detail.value.posts[0].content, 'old detail')

  harness.queueDetail(Promise.reject(new Error('detail request failed')))
  harness.queueList(Promise.resolve(listResponse({ active: [entry], selected: entry })))
  await board.refresh()
  await flushMicrotasks()
  assert.equal(board.detailFailed.value, true)
  assert.equal(board.detailLoading.value, false)
  assert.equal(board.detail.value.work_id, 20)
  assert.equal(board.detail.value.posts[0].content, 'old detail')
  assert.equal(board.detail.value.result_text, 'old result')
  assert.equal(board.nextBeforePostId.value, 10)

  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 20,
    status: 'succeeded',
    completed: true,
    posts: [makePost(20, 20, { content: 'new detail' })],
    next_before_post_id: null,
    result_text: 'new result',
    reason: null,
    ended_without_reply: false
  })))
  harness.queueList(Promise.resolve(listResponse({ active: [entry], selected: entry })))
  await board.refresh()
  await flushMicrotasks()
  assert.equal(board.detailFailed.value, false)
  assert.equal(board.detail.value.posts[0].content, 'new detail')
  assert.equal(board.detail.value.result_text, 'new result')
})

test('detail pages deduplicate and sort posts, keep loaded work state, preserve records on refresh, and reset on disconnected latest pages', async t => {
  const timers = createManualTimerQueue()
  const harness = createApi()
  const board = createSessionSubagentBoard({
    api: harness.api,
    schedule: timers.schedule,
    cancel: timers.cancel
  })
  t.after(() => board.dispose())

  const entry = makeEntry('agent-a', 20)
  harness.queueList(Promise.resolve(listResponse({ active: [entry] })))
  board.setScope('session-a', 'profile-a')
  board.setEnabled(true)
  await flushMicrotasks()

  const initialDetail = createDeferred()
  harness.queueDetail(initialDetail.promise)
  const selection = board.selectSubagent('agent-a')
  initialDetail.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 20,
    status: 'running',
    completed: false,
    rounds: [
      { work_id: 10, created_at: '2026-11-14T11:59:00Z' },
      { work_id: 20, created_at: '2026-11-14T12:00:00Z' }
    ],
    posts: [
      makePost(6, 20, { content: 'first-six' }),
      makePost(4, 20),
      makePost(6, 20, { content: 'last-six' }),
      makePost(5, 20)
    ],
    next_before_post_id: 4,
    result_text: 'current result',
    reason: null,
    ended_without_reply: false
  }))
  await selection
  assert.deepEqual(board.detail.value.posts.map(post => post.id), [4, 5, 6])
  assert.equal(board.detail.value.posts.find(post => post.id === 6).content, 'last-six')

  const olderPage = createDeferred()
  harness.queueDetail(olderPage.promise)
  const olderRequest = board.loadOlderPosts()
  await flushMicrotasks()
  assert.deepEqual(harness.detailCalls[1].params, {
    before_post_id: 4,
    work_id: 20
  })
  olderPage.resolve(postPageResponse({
    work_id: 20,
    posts: [
      makePost(3, 20),
      makePost(4, 20, { content: 'older-four' })
    ],
    next_before_post_id: 1
  }))
  await olderRequest
  assert.deepEqual(board.detail.value.posts.map(post => post.id), [3, 4, 5, 6])
  assert.equal(board.detail.value.posts.find(post => post.id === 4).content, 'older-four')
  assert.equal(board.detail.value.status, 'running')
  assert.equal(board.detail.value.completed, false)
  assert.equal(board.detail.value.result_text, 'current result')
  assert.equal(board.detail.value.reason, null)
  assert.equal(board.detail.value.ended_without_reply, false)
  assert.equal(board.nextBeforePostId.value, 1)

  const refreshedDetail = createDeferred()
  harness.queueDetail(refreshedDetail.promise)
  harness.queueList(Promise.resolve(listResponse({ active: [entry], selected: entry })))
  const refresh = board.refresh()
  await refresh
  assert.equal(harness.detailCalls.length, 3)
  refreshedDetail.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 20,
    status: 'succeeded',
    completed: true,
    posts: [makePost(5, 20, { content: 'latest-five' }), makePost(6, 20)],
    next_before_post_id: null,
    result_text: 'completed result',
    reason: null,
    ended_without_reply: false
  }))
  await flushMicrotasks()
  assert.deepEqual(board.detail.value.posts.map(post => post.id), [3, 4, 5, 6])
  assert.equal(board.nextBeforePostId.value, 1)
  assert.equal(board.detail.value.next_before_post_id, 1)
  assert.equal(board.detail.value.completed, true)

  const finalOlderPage = createDeferred()
  harness.queueDetail(finalOlderPage.promise)
  const finalOlderRequest = board.loadOlderPosts()
  await flushMicrotasks()
  assert.deepEqual(harness.detailCalls[3].params, {
    before_post_id: 1,
    work_id: 20
  })
  finalOlderPage.resolve(postPageResponse({
    work_id: 20,
    posts: [makePost(2, 20)],
    next_before_post_id: null
  }))
  await finalOlderRequest
  assert.deepEqual(board.detail.value.posts.map(post => post.id), [2, 3, 4, 5, 6])
  assert.equal(board.nextBeforePostId.value, null)
  assert.equal(board.detail.value.next_before_post_id, null)

  const completedCursorRefresh = createDeferred()
  harness.queueDetail(completedCursorRefresh.promise)
  harness.queueList(Promise.resolve(listResponse({ active: [entry], selected: entry })))
  const completedRefresh = board.refresh()
  await completedRefresh
  completedCursorRefresh.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 20,
    status: 'succeeded',
    completed: true,
    posts: [makePost(5, 20), makePost(6, 20)],
    next_before_post_id: 4,
    result_text: 'completed result',
    reason: null,
    ended_without_reply: false
  }))
  await flushMicrotasks()
  assert.deepEqual(board.detail.value.posts.map(post => post.id), [2, 3, 4, 5, 6])
  assert.equal(board.nextBeforePostId.value, null)
  assert.equal(board.detail.value.next_before_post_id, null)

  const disconnectedDetail = createDeferred()
  harness.queueDetail(disconnectedDetail.promise)
  harness.queueList(Promise.resolve(listResponse({ active: [entry], selected: entry })))
  const disconnectedRefresh = board.refresh()
  await disconnectedRefresh
  disconnectedDetail.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 20,
    status: 'running',
    completed: false,
    posts: [makePost(10, 20), makePost(11, 20)],
    next_before_post_id: 9,
    result_text: null,
    reason: null,
    ended_without_reply: false
  }))
  await flushMicrotasks()
  assert.deepEqual(board.detail.value.posts.map(post => post.id), [10, 11])
  assert.equal(board.nextBeforePostId.value, 9)
  assert.equal(board.detail.value.posts.some(post => post.id === 3), false)
})

test('new rounds replace old posts, different agents never share details, transitions do not duplicate cards, and missing selections clear', async t => {
  const timers = createManualTimerQueue()
  const harness = createApi()
  const board = createSessionSubagentBoard({
    api: harness.api,
    schedule: timers.schedule,
    cancel: timers.cancel
  })
  t.after(() => board.dispose())

  const agentA10 = makeEntry('agent-a', 10)
  const agentB30 = makeEntry('agent-b', 30, { completed: true })
  harness.queueList(Promise.resolve(listResponse({
    active: [agentA10],
    recent: [agentB30]
  })))
  board.setScope('session-a', 'profile-a')
  board.setEnabled(true)
  await flushMicrotasks()

  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 10,
    posts: [makePost(10, 10)]
  })))
  await board.selectSubagent('agent-a')
  assert.equal(board.detail.value.work_id, 10)

  const newRoundDetail = createDeferred()
  harness.queueDetail(newRoundDetail.promise)
  const agentA11 = makeEntry('agent-a', 11)
  harness.queueList(Promise.resolve(listResponse({
    active: [agentA11],
    recent: [agentB30],
    selected: agentA11
  })))
  await board.refresh()
  newRoundDetail.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 11,
    rounds: [
      { work_id: 10, created_at: '2026-11-14T12:00:00Z' },
      { work_id: 11, created_at: '2026-11-14T12:01:00Z' }
    ],
    posts: [makePost(11, 11)]
  }))
  await flushMicrotasks()
  assert.equal(board.detail.value.work_id, 11)
  assert.deepEqual(board.detail.value.posts.map(post => post.id), [11])

  const staleAgentA = createDeferred()
  harness.queueDetail(staleAgentA.promise)
  const staleRequest = board.selectSubagent('agent-a')
  await flushMicrotasks()
  const staleAgentACall = harness.detailCalls.at(-1)

  const agentBDetail = createDeferred()
  harness.queueDetail(agentBDetail.promise)
  const selectB = board.selectSubagent('agent-b')
  await flushMicrotasks()
  assert.equal(staleAgentACall.signal.aborted, true)
  staleAgentA.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 11,
    posts: [makePost(111, 11)]
  }))
  await staleRequest
  assert.equal(board.detail.value, null)

  agentBDetail.resolve(detailResponse({
    subagent_id: 'agent-b',
    work_id: 30,
    posts: [makePost(300, 30)]
  }))
  await selectB
  assert.equal(board.selectedId.value, 'agent-b')
  assert.equal(board.detail.value.subagent_id, 'agent-b')
  assert.deepEqual(board.detail.value.posts.map(post => post.id), [300])
  assert.equal(board.detail.value.posts.some(post => post.id === 111), false)

  harness.queueList(Promise.resolve(listResponse({
    active: [],
    recent: [makeEntry('agent-a', 11, { completed: true })]
  })))
  await board.refresh()
  assert.deepEqual(
    [...board.active.value, ...board.recent.value].map(entry => entry.subagent_id),
    ['agent-a']
  )
  assert.equal(new Set([...board.active.value, ...board.recent.value].map(entry => entry.subagent_id)).size, 1)
  assert.equal(board.selectedId.value, null)
  assert.equal(board.detail.value, null)

  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 11,
    posts: [makePost(1100, 11)]
  })))
  await board.selectSubagent('agent-a')
  assert.equal(board.selectedId.value, 'agent-a')

  harness.queueList(Promise.resolve(listResponse({ active: [], recent: [] })))
  await board.refresh()
  assert.equal(board.selectedId.value, null)
  assert.equal(board.selectedWorkId.value, null)
  assert.equal(board.detail.value, null)
})

test('load more increases the recent limit by ten up to one hundred, stays capped, and resets per scope', async t => {
  const timers = createManualTimerQueue()
  const harness = createApi()
  const board = createSessionSubagentBoard({
    api: harness.api,
    schedule: timers.schedule,
    cancel: timers.cancel
  })
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

test('auto-refreshes latest detail only when its selected summary changes', async t => {
  const timers = createManualTimerQueue()
  const harness = createApi()
  const board = createSessionSubagentBoard({
    api: harness.api,
    schedule: timers.schedule,
    cancel: timers.cancel
  })
  t.after(() => board.dispose())

  const runningEntry = makeEntry('agent-a', 20, {
    latest_post_id: 1,
    updated_at: '2026-11-14T12:01:00Z'
  })
  harness.queueList(Promise.resolve(listResponse({
    active: [runningEntry]
  })))
  board.setScope('session-a', 'profile-a')
  board.setEnabled(true)
  await flushMicrotasks()

  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 20,
    posts: [makePost(1, 20, { content: 'first post' })]
  })))
  await board.selectSubagent('agent-a')
  assert.equal(harness.detailCalls.length, 1)

  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 20,
    posts: [makePost(1, 20, { content: 'first post' })]
  })))
  await board.selectRound(20)
  assert.equal(board.selectedWorkId.value, 20)
  assert.deepEqual(harness.detailCalls[1].params, { work_id: 20 })
  assert.equal(board.detail.value.work_id, 20)
  assert.equal(harness.detailCalls.length, 2)

  const renewedEntry = {
    ...runningEntry,
    updated_at: '2026-11-14T12:02:00Z'
  }
  harness.queueList(Promise.resolve(listResponse({
    active: [renewedEntry],
    selected: renewedEntry
  })))
  await timers.tick()
  assert.equal(harness.detailCalls.length, 2)
  assert.equal(board.selectedEntry.value.updated_at, renewedEntry.updated_at)
  assert.deepEqual({
    work_id: board.selectedEntry.value.work_id,
    latest_post_id: board.selectedEntry.value.latest_post_id,
    status: board.selectedEntry.value.status,
    completed: board.selectedEntry.value.completed
  }, {
    work_id: 20,
    latest_post_id: 1,
    status: 'running',
    completed: false
  })

  const newPostEntry = {
    ...renewedEntry,
    latest_post_id: 2,
    updated_at: '2026-11-14T12:03:00Z'
  }
  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 20,
    posts: [makePost(2, 20, { content: 'second post' })]
  })))
  harness.queueList(Promise.resolve(listResponse({
    active: [newPostEntry],
    selected: newPostEntry
  })))
  await timers.tick()
  await flushMicrotasks()
  assert.equal(harness.detailCalls.length, 3)
  assert.deepEqual(harness.detailCalls[2].params, { work_id: 20 })
  assert.equal(board.detail.value.posts.find(post => post.id === 2).content, 'second post')

  const newRoundEntry = makeEntry('agent-a', 21, {
    latest_post_id: 4,
    updated_at: '2026-11-14T12:04:00Z'
  })
  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 20,
    status: 'succeeded',
    completed: true,
    rounds: [makeRound(21), makeRound(20)],
    posts: [makePost(2, 20), makePost(3, 20)],
    result_text: 'final report'
  })))
  harness.queueList(Promise.resolve(listResponse({
    active: [newRoundEntry],
    selected: newRoundEntry
  })))
  await timers.tick()
  await flushMicrotasks()
  assert.equal(harness.detailCalls.length, 4)
  assert.deepEqual(harness.detailCalls[3].params, { work_id: 20 })
  assert.equal(board.selectedEntry.value.work_id, 21)
  assert.equal(board.selectedWorkId.value, 20)
  assert.equal(board.detail.value.work_id, 20)
  assert.equal(board.detail.value.completed, true)
  assert.equal(board.detail.value.result_text, 'final report')

  const newRoundRenewal = {
    ...newRoundEntry,
    updated_at: '2026-11-14T12:05:00Z'
  }
  harness.queueList(Promise.resolve(listResponse({
    active: [newRoundRenewal],
    selected: newRoundRenewal
  })))
  await timers.tick()
  await flushMicrotasks()
  assert.equal(harness.detailCalls.length, 4)
  assert.equal(board.selectedEntry.value.work_id, 21)
  assert.equal(board.detail.value.result_text, 'final report')
})

test('keeps an explicitly selected historical detail while latest list data arrives and manual retry runs', async t => {
  const timers = createManualTimerQueue()
  const harness = createApi()
  const board = createSessionSubagentBoard({
    api: harness.api,
    schedule: timers.schedule,
    cancel: timers.cancel
  })
  t.after(() => board.dispose())

  const latestEntry = makeEntry('agent-a', 20, { latest_post_id: 2 })
  harness.queueList(Promise.resolve(listResponse({
    active: [latestEntry]
  })))
  board.setScope('session-a', 'profile-a')
  board.setEnabled(true)
  await flushMicrotasks()

  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 20,
    posts: [makePost(2, 20)]
  })))
  await board.selectSubagent('agent-a')

  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 10,
    status: 'succeeded',
    completed: true,
    rounds: [
      { work_id: 10, created_at: '2026-11-14T11:59:00Z' },
      { work_id: 20, created_at: '2026-11-14T12:00:00Z' }
    ],
    posts: [makePost(1, 10, { content: 'historical detail' })]
  })))
  await board.selectRound(10)
  assert.equal(board.selectedWorkId.value, 10)
  assert.equal(board.detail.value.work_id, 10)

  harness.queueDetail(Promise.reject(new Error('historical detail retry failed')))
  await board.refreshDetail()
  assert.equal(board.detailFailed.value, true)
  assert.equal(board.detail.value.work_id, 10)

  const listInFlight = createDeferred()
  harness.queueList(listInFlight.promise)
  const listRefresh = timers.tick()
  await flushMicrotasks()
  assert.equal(board.loading.value, true)

  const retriedDetail = createDeferred()
  harness.queueDetail(retriedDetail.promise)
  const detailRetry = board.refreshDetail()
  await flushMicrotasks()
  assert.equal(harness.detailCalls.length, 4)
  assert.deepEqual(harness.detailCalls[3].params, { work_id: 10 })
  retriedDetail.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 10,
    status: 'succeeded',
    completed: true,
    posts: [makePost(2, 10, { content: 'retried historical detail' })]
  }))
  await detailRetry
  assert.equal(board.detailFailed.value, false)
  assert.equal(board.detail.value.work_id, 10)
  assert.equal(board.detail.value.posts.find(post => post.id === 2).content, 'retried historical detail')

  listInFlight.resolve(listResponse({
    active: [latestEntry],
    selected: latestEntry
  }))
  await listRefresh
  await flushMicrotasks()
  assert.equal(harness.detailCalls.length, 4)
  assert.equal(board.selectedEntry.value.work_id, 20)
  assert.equal(board.selectedWorkId.value, 10)
  assert.equal(board.detail.value.work_id, 10)
  assert.equal(board.detail.value.posts.find(post => post.id === 2).content, 'retried historical detail')
})

test('round pages preserve the loaded directory across historical detail and retry independently', async t => {
  const timers = createManualTimerQueue()
  const harness = createApi()
  const board = createSessionSubagentBoard({
    api: harness.api,
    schedule: timers.schedule,
    cancel: timers.cancel
  })
  t.after(() => board.dispose())

  const latestEntry = makeEntry('agent-a', 60)
  const latestRounds = Array.from({ length: 20 }, (_, index) => makeRound(60 - index))
  const olderRounds = Array.from({ length: 20 }, (_, index) => makeRound(40 - index))
  harness.queueList(Promise.resolve(listResponse({ active: [latestEntry] })))
  board.setScope('session-a', 'profile-a')
  board.setEnabled(true)
  await flushMicrotasks()

  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 60,
    rounds: latestRounds,
    next_before_round_id: 41,
    posts: [makePost(300, 60)]
  })))
  await board.selectSubagent('agent-a')
  assert.deepEqual(board.rounds.value.map(round => round.work_id), latestRounds.map(round => round.work_id))
  assert.equal(board.nextBeforeRoundId.value, 41)

  const olderPage = createDeferred()
  harness.queueDetail(olderPage.promise)
  const olderRequest = board.loadOlderRounds()
  await flushMicrotasks()
  assert.deepEqual(harness.detailCalls[1].params, { before_round_id: 41 })
  olderPage.resolve(roundPageResponse({
    rounds: olderRounds,
    next_before_round_id: 21
  }))
  await olderRequest
  assert.deepEqual(board.rounds.value.map(round => round.work_id), [
    ...latestRounds.map(round => round.work_id),
    ...olderRounds.map(round => round.work_id)
  ])
  assert.equal(board.nextBeforeRoundId.value, 21)

  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 35,
    rounds: latestRounds,
    next_before_round_id: 41,
    posts: [makePost(350, 35)]
  })))
  await board.selectRound(35)
  assert.equal(board.detail.value.work_id, 35)
  assert.deepEqual(board.rounds.value.map(round => round.work_id), [
    ...latestRounds.map(round => round.work_id),
    ...olderRounds.map(round => round.work_id)
  ])
  assert.equal(board.nextBeforeRoundId.value, 21)

  harness.queueDetail(Promise.reject(new Error('detail failure')))
  await board.refreshDetail()
  assert.equal(board.detailFailed.value, true)
  assert.equal(board.detail.value.posts[0].id, 350)

  harness.queueDetail(Promise.reject(new Error('round page failure')))
  await board.loadOlderRounds()
  assert.equal(board.roundsFailed.value, true)
  assert.equal(board.detailFailed.value, true)
  assert.equal(board.detail.value.work_id, 35)
  assert.equal(board.nextBeforeRoundId.value, 21)

  harness.queueDetail(Promise.resolve(roundPageResponse({
    rounds: [20, 19, 18].map(makeRound),
    next_before_round_id: null
  })))
  await board.loadOlderRounds()
  assert.equal(board.roundsFailed.value, false)
  assert.equal(board.detailFailed.value, true)
  assert.equal(board.detail.value.work_id, 35)
  assert.deepEqual(board.rounds.value.slice(-3).map(round => round.work_id), [20, 19, 18])
  assert.equal(board.nextBeforeRoundId.value, null)
})

test('round pages ignore cancelled late responses after a session or agent switch', async t => {
  const timers = createManualTimerQueue()
  const harness = createApi()
  const board = createSessionSubagentBoard({
    api: harness.api,
    schedule: timers.schedule,
    cancel: timers.cancel
  })
  t.after(() => board.dispose())

  const agentA = makeEntry('agent-a', 30)
  harness.queueList(Promise.resolve(listResponse({ active: [agentA] })))
  board.setScope('session-a', 'profile-a')
  board.setEnabled(true)
  await flushMicrotasks()

  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 30,
    rounds: Array.from({ length: 20 }, (_, index) => makeRound(30 - index)),
    next_before_round_id: 11,
    posts: [makePost(300, 30)]
  })))
  await board.selectSubagent('agent-a')

  const sessionPage = createDeferred()
  harness.queueDetail(sessionPage.promise)
  const sessionPageRequest = board.loadOlderRounds()
  await flushMicrotasks()
  const sessionPageCall = harness.detailCalls.at(-1)

  const agentB = makeEntry('agent-b', 40)
  const agentC = makeEntry('agent-c', 50)
  harness.queueList(Promise.resolve(listResponse({
    profile_id: 2,
    active: [agentB, agentC]
  })))
  board.setScope('session-b', 'profile-b')
  await flushMicrotasks()
  assert.equal(sessionPageCall.signal.aborted, true)
  assert.deepEqual(board.rounds.value, [])
  assert.equal(board.nextBeforeRoundId.value, null)

  sessionPage.resolve(roundPageResponse({
    rounds: [10, 9, 8].map(makeRound),
    next_before_round_id: null
  }))
  await sessionPageRequest
  assert.deepEqual(board.rounds.value, [])
  assert.equal(board.nextBeforeRoundId.value, null)

  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-b',
    work_id: 40,
    rounds: Array.from({ length: 20 }, (_, index) => makeRound(40 - index)),
    next_before_round_id: 21,
    posts: [makePost(400, 40)]
  })))
  await board.selectSubagent('agent-b')

  const agentPage = createDeferred()
  harness.queueDetail(agentPage.promise)
  const agentPageRequest = board.loadOlderRounds()
  await flushMicrotasks()
  const agentPageCall = harness.detailCalls.at(-1)

  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-c',
    work_id: 50,
    rounds: Array.from({ length: 20 }, (_, index) => makeRound(50 - index)),
    next_before_round_id: 31,
    posts: [makePost(500, 50)]
  })))
  await board.selectSubagent('agent-c')
  assert.equal(agentPageCall.signal.aborted, true)
  assert.equal(board.selectedId.value, 'agent-c')
  assert.equal(board.detail.value.work_id, 50)
  assert.equal(board.nextBeforeRoundId.value, 31)

  agentPage.resolve(roundPageResponse({
    rounds: [20, 19, 18].map(makeRound),
    next_before_round_id: null
  }))
  await agentPageRequest
  assert.equal(board.selectedId.value, 'agent-c')
  assert.deepEqual(board.rounds.value.slice(0, 3).map(round => round.work_id), [50, 49, 48])
  assert.equal(board.rounds.value.some(round => round.work_id === 20), false)
  assert.equal(board.nextBeforeRoundId.value, 31)
})

test('keeps an explicitly selected old detail while a refreshed first page fills the round directory gap', async t => {
  const timers = createManualTimerQueue()
  const harness = createApi()
  const board = createSessionSubagentBoard({
    api: harness.api,
    schedule: timers.schedule,
    cancel: timers.cancel
  })
  t.after(() => board.dispose())

  const latestEntry = makeEntry('agent-a', 60)
  const initialLatestRounds = Array.from({ length: 20 }, (_, index) => makeRound(60 - index))
  const initialOlderRounds = Array.from({ length: 20 }, (_, index) => makeRound(40 - index))
  harness.queueList(Promise.resolve(listResponse({ active: [latestEntry] })))
  board.setScope('session-a', 'profile-a')
  board.setEnabled(true)
  await flushMicrotasks()

  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 60,
    rounds: initialLatestRounds,
    next_before_round_id: 41,
    posts: [makePost(600, 60)]
  })))
  await board.selectSubagent('agent-a')

  harness.queueDetail(Promise.resolve(roundPageResponse({
    rounds: initialOlderRounds,
    next_before_round_id: 21
  })))
  await board.loadOlderRounds()
  assert.equal(board.rounds.value.some(round => round.work_id === 35), true)

  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 35,
    status: 'succeeded',
    completed: true,
    rounds: initialLatestRounds,
    next_before_round_id: 41,
    posts: [makePost(350, 35, { content: 'historical detail' })]
  })))
  await board.selectRound(35)
  assert.equal(board.detail.value.work_id, 35)

  const refreshedLatestRounds = Array.from({ length: 20 }, (_, index) => makeRound(85 - index))
  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 35,
    status: 'succeeded',
    completed: true,
    rounds: refreshedLatestRounds,
    next_before_round_id: 66,
    posts: [makePost(350, 35, { content: 'historical detail' })]
  })))
  await board.refreshDetail()
  assert.deepEqual(harness.detailCalls.at(-1).params, { work_id: 35 })
  assert.equal(board.detail.value.work_id, 35)
  assert.equal(board.detail.value.posts[0].content, 'historical detail')
  assert.deepEqual(board.rounds.value.slice(0, 20).map(round => round.work_id), refreshedLatestRounds.map(round => round.work_id))
  assert.equal(board.rounds.value.some(round => round.work_id === 35), true)
  assert.equal(board.nextBeforeRoundId.value, 66)

  const gapRounds = Array.from({ length: 20 }, (_, index) => makeRound(65 - index))
  harness.queueDetail(Promise.resolve(roundPageResponse({
    rounds: gapRounds,
    next_before_round_id: 46
  })))
  await board.loadOlderRounds()
  assert.deepEqual(harness.detailCalls.at(-1).params, { before_round_id: 66 })
  assert.equal(board.nextBeforeRoundId.value, 46)
  assert.equal(board.detail.value.work_id, 35)

  harness.queueDetail(Promise.resolve(roundPageResponse({
    rounds: Array.from({ length: 20 }, (_, index) => makeRound(45 - index)),
    next_before_round_id: 26
  })))
  await board.loadOlderRounds()
  assert.deepEqual(harness.detailCalls.at(-1).params, { before_round_id: 46 })

  harness.queueDetail(Promise.resolve(roundPageResponse({
    rounds: Array.from({ length: 8 }, (_, index) => makeRound(25 - index)),
    next_before_round_id: null
  })))
  await board.loadOlderRounds()
  assert.deepEqual(harness.detailCalls.at(-1).params, { before_round_id: 26 })

  const roundIds = board.rounds.value.map(round => round.work_id)
  assert.deepEqual(roundIds, Array.from({ length: 68 }, (_, index) => 85 - index))
  assert.equal(new Set(roundIds).size, roundIds.length)
  assert.equal(board.nextBeforeRoundId.value, null)
  assert.equal(board.detail.value.work_id, 35)
})
