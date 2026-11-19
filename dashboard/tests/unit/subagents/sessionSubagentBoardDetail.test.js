import assert from 'node:assert/strict'
import test from 'node:test'

import { createSessionSubagentBoard } from '../../../src/composables/chat/sessionSubagentBoard.js'
import {
  createApi,
  createDeferred,
  detailResponse,
  flushMicrotasks,
  listResponse,
  makeEntry,
  makePost,
  makeRound,
  postPageResponse
} from './sessionSubagentBoardHarness.js'

test('a selected detail refresh adds one follow-up when a newer summary arrives in flight', async t => {
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
  t.after(() => board.dispose())

  const firstEntry = makeEntry('agent-a', 20, { latest_post_id: 1 })
  harness.queueList(Promise.resolve(listResponse({ active: [firstEntry] })))
  board.setScope('session-a', 'profile-a', 0, 1)
  board.setEnabled(true)
  await flushMicrotasks()

  harness.queueDetail(Promise.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 20,
    posts: [makePost(1, 20)]
  })))
  await board.selectSubagent('agent-a')

  const laggingDetail = createDeferred()
  const followUpDetail = createDeferred()
  harness.queueDetail(laggingDetail.promise)
  const laggingRequest = board.refreshDetail()
  await flushMicrotasks()

  const newerEntry = { ...firstEntry, latest_post_id: 2 }
  harness.queueDetail(followUpDetail.promise)
  harness.queueList(Promise.resolve(listResponse({
    active: [newerEntry],
    selected: newerEntry
  })))
  board.observeActivity('session-a', 1, 1)
  await flushMicrotasks()
  assert.equal(harness.listCalls.length, 2)
  assert.equal(harness.detailCalls.length, 2)

  laggingDetail.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 20,
    posts: [makePost(1, 20, { content: 'lagging detail' })]
  }))
  await laggingRequest
  await flushMicrotasks()
  assert.equal(harness.detailCalls.length, 3)

  followUpDetail.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 20,
    posts: [makePost(2, 20, { content: 'updated detail' })]
  }))
  await flushMicrotasks()
  assert.equal(harness.detailCalls.length, 3)
  assert.equal(board.detail.value.posts.find(post => post.id === 2).content, 'updated detail')
})

test('does not refresh first-open detail when another task changes in the same list snapshot', async t => {
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
  t.after(() => board.dispose())

  const firstEntry = makeEntry('agent-a', 20, { latest_post_id: 1 })
  const otherEntry = makeEntry('agent-b', 30, { latest_post_id: 1 })
  harness.queueList(Promise.resolve(listResponse({
    active: [firstEntry, otherEntry]
  })))
  board.setScope('session-a', 'profile-a', 0, 1)
  board.setEnabled(true)
  await flushMicrotasks()

  const firstDetail = createDeferred()
  harness.queueDetail(firstDetail.promise)
  const selection = board.selectSubagent('agent-a')
  await flushMicrotasks()
  assert.equal(harness.detailCalls.length, 1)

  const unchangedSelected = { ...firstEntry }
  const progressedOther = {
    ...otherEntry,
    latest_post_id: 2,
    updated_at: '2026-11-14T12:03:00Z'
  }
  harness.queueList(Promise.resolve(listResponse({
    active: [unchangedSelected, progressedOther],
    selected: { ...unchangedSelected }
  })))
  board.observeActivity('session-a', 1, 1)
  await flushMicrotasks()
  assert.equal(harness.listCalls.length, 2)
  assert.equal(harness.detailCalls.length, 1)
  assert.deepEqual(board.selectedEntry.value, firstEntry)
  assert.equal(board.active.value.find(entry => entry.subagent_id === 'agent-b').latest_post_id, 2)

  firstDetail.resolve(detailResponse({
    subagent_id: 'agent-a',
    work_id: 20,
    posts: [makePost(1, 20, { content: 'first-open detail' })]
  }))
  await selection
  await flushMicrotasks()
  assert.equal(harness.detailCalls.length, 1)
  assert.equal(board.detail.value.posts[0].content, 'first-open detail')
  assert.deepEqual(board.selectedEntry.value, firstEntry)
})

for (const scenario of [
  {
    title: 'a detail failure under a stable snapshot retries detail only',
    revision: 0,
    retryThroughList: false
  },
  {
    title: 'a detail failure with unchanged selected summary retries through a newer list snapshot',
    revision: 1,
    retryThroughList: true
  }
]) {
  test(scenario.title, async t => {
    const harness = createApi()
    const board = createSessionSubagentBoard({ api: harness.api })
    t.after(() => board.dispose())

    const entry = makeEntry('agent-a', 20)
    const otherEntry = makeEntry('agent-b', 30, { latest_post_id: 1 })
    harness.queueList(Promise.resolve(listResponse({
      active: [entry, otherEntry]
    })))
    board.setScope('session-a', 'profile-a', 0, 1)
    board.setEnabled(true)
    await flushMicrotasks()

    harness.queueDetail(Promise.resolve(detailResponse({
      subagent_id: 'agent-a',
      work_id: 20,
      posts: [makePost(1, 20, { content: 'old detail' })],
      result_text: 'old result'
    })))
    await board.selectSubagent('agent-a')

    harness.queueDetail(Promise.reject(new Error('detail request failed')))
    await board.refreshDetail()
    assert.equal(board.detailFailed.value, true)
    assert.equal(harness.listCalls.length, 1)
    assert.equal(harness.detailCalls.length, 2)
    assert.equal(board.detail.value.posts[0].content, 'old detail')
    assert.equal(board.detail.value.result_text, 'old result')

    const retriedDetail = createDeferred()
    harness.queueDetail(retriedDetail.promise)
    if (scenario.retryThroughList) {
      harness.queueList(Promise.resolve(listResponse({
        active: [entry, {
          ...otherEntry,
          latest_post_id: 2,
          updated_at: '2026-11-14T12:03:00Z'
        }],
        selected: { ...entry }
      })))
    }
    board.observeActivity('session-a', scenario.revision, 1)
    await flushMicrotasks()
    assert.equal(harness.listCalls.length, scenario.retryThroughList ? 2 : 1)
    assert.equal(harness.detailCalls.length, 3)
    if (scenario.retryThroughList) {
      assert.equal(harness.listCalls[1].params.selected_subagent_id, 'agent-a')
      assert.deepEqual(board.selectedEntry.value, entry)
      assert.equal(board.active.value.find(item => item.subagent_id === 'agent-b').latest_post_id, 2)
    }
    assert.equal(board.detail.value.posts[0].content, 'old detail')
    assert.equal(board.detail.value.result_text, 'old result')

    retriedDetail.resolve(detailResponse({
      subagent_id: 'agent-a',
      work_id: 20,
      posts: [makePost(2, 20, { content: 'recovered detail' })],
      result_text: 'recovered result'
    }))
    await flushMicrotasks()
    assert.equal(board.detailFailed.value, false)
    assert.equal(board.detail.value.posts.find(post => post.id === 2).content, 'recovered detail')
    assert.equal(board.detail.value.result_text, 'recovered result')
  })
}

test('a detail failure keeps the previous detail until a later refresh succeeds', async t => {
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
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
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
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
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
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

test('auto-refreshes latest detail only when its selected summary changes', async t => {
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
  t.after(() => board.dispose())

  const runningEntry = makeEntry('agent-a', 20, {
    latest_post_id: 1,
    updated_at: '2026-11-14T12:01:00Z'
  })
  harness.queueList(Promise.resolve(listResponse({
    active: [runningEntry]
  })))
  board.setScope('session-a', 'profile-a', 0, 1)
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
  board.observeActivity('session-a', 1)
  await flushMicrotasks()
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
  board.observeActivity('session-a', 2)
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
  board.observeActivity('session-a', 3)
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
  board.observeActivity('session-a', 4)
  await flushMicrotasks()
  assert.equal(harness.detailCalls.length, 4)
  assert.equal(board.selectedEntry.value.work_id, 21)
  assert.equal(board.detail.value.result_text, 'final report')
})
