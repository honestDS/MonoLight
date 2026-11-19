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
  roundPageResponse
} from './sessionSubagentBoardHarness.js'

test('selection and round changes abort stale detail requests, follow the latest round, and pass historical work ids', async t => {
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
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

test('keeps an explicitly selected historical detail while latest list data arrives and manual retry runs', async t => {
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
  t.after(() => board.dispose())

  const latestEntry = makeEntry('agent-a', 20, { latest_post_id: 2 })
  harness.queueList(Promise.resolve(listResponse({
    active: [latestEntry]
  })))
  board.setScope('session-a', 'profile-a', 0, 1)
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
  board.observeActivity('session-a', 1)
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
  await flushMicrotasks()
  assert.equal(harness.detailCalls.length, 4)
  assert.equal(board.selectedEntry.value.work_id, 20)
  assert.equal(board.selectedWorkId.value, 10)
  assert.equal(board.detail.value.work_id, 10)
  assert.equal(board.detail.value.posts.find(post => post.id === 2).content, 'retried historical detail')
})

test('round pages preserve the loaded directory across historical detail and retry independently', async t => {
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
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
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
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
  const harness = createApi()
  const board = createSessionSubagentBoard({ api: harness.api })
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
