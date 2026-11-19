import { test } from 'node:test'
import assert from 'node:assert/strict'

import { createHarness, createSession } from '../../chatDraftHarness.js'
import {
  createMainMetadata,
  receiveWs,
  selectSession,
  snapshotMetadata
} from './sessionCompositionHarness.js'

test('composed reconnect refreshes persisted task totals without replaying task events', async t => {
  const mainMetadata = createMainMetadata()
  const taskTotals = {
    total_input_tokens: 50120,
    total_cached_tokens: 270,
    total_output_tokens: 207,
    cache_hit_rate: 270 / 50120
  }
  const expectedMetadata = { ...mainMetadata, ...taskTotals }
  const taskContent = 'task completed while disconnected'
  const harness = await createHarness(t, {
    sessions: [createSession('A', { source: 'ws', llm_request_metadata: mainMetadata })],
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness

  await harness.flush()
  await receiveWs(harness, {
    type: 'llm_request_metadata',
    session_id: 'A',
    ...mainMetadata
  })

  const sentBeforeReconnect = harness.wsManager.sent.length
  await receiveWs(harness, { type: 'connection_closed' })
  const serverSession = harness.sessions.find(item => item.session_id === 'A')
  serverSession.llm_request_metadata = { ...expectedMetadata }
  serverSession.messages.push({
    id: 1201,
    role: 'assistant',
    response_id: 'disconnected-task-response',
    content: taskContent
  })

  await receiveWs(harness, { type: 'connection_reopened' })
  const resumeRequest = harness.wsManager.sent
    .slice(sentBeforeReconnect)
    .find(item => item.type === 'resume' && item.session_id === 'A')
  assert.ok(resumeRequest)
  assert.equal(resumeRequest.history_message_id, 0)

  await receiveWs(harness, { type: 'resume_complete', session_id: 'A' })
  assert.deepEqual(snapshotMetadata(chat.llmRequestMetadata.value), expectedMetadata)
  assert.equal(chat.llmRequestMetadata.value.total_input_tokens, 50120)
  assert.equal(chat.llmRequestMetadata.value.input_tokens, 50000)
  assert.equal(chat.llmRequestMetadata.value.context_window_tokens, 131072)
  assert.equal(chat.llmRequestMetadata.value.work_sequence_no, 2)
  assert.equal(chat.messages.value.filter(message => message.content === taskContent).length, 1)

  serverSession.llm_request_metadata = null
  await receiveWs(harness, { type: 'resume_complete', session_id: 'A' })
  assert.deepEqual(snapshotMetadata(chat.llmRequestMetadata.value), expectedMetadata)
  assert.equal(chat.llmRequestMetadata.value.total_input_tokens, 50120)
  assert.equal(chat.messages.value.filter(message => message.content === taskContent).length, 1)
  serverSession.llm_request_metadata = { ...expectedMetadata }

  await receiveWs(harness, {
    type: 'llm_request_metadata',
    session_id: 'A',
    ...mainMetadata,
    event_sequence_no: 3,
    total_input_tokens: 50000,
    total_cached_tokens: 250,
    total_output_tokens: 200,
    cache_hit_rate: 250 / 50000
  })
  assert.deepEqual(snapshotMetadata(chat.llmRequestMetadata.value), expectedMetadata)
})

test('composed reconnect preserves newer metadata across stale session snapshots', async t => {
  const mainMetadata = createMainMetadata()
  const persistedTotals = {
    total_input_tokens: 50120,
    total_cached_tokens: 270,
    total_output_tokens: 207,
    cache_hit_rate: 270 / 50120
  }
  const persistedMetadata = { ...mainMetadata, ...persistedTotals }
  const expectedNextMetadata = {
    ...persistedMetadata,
    input_tokens: 60000,
    output_tokens: 191,
    cached_tokens: 300,
    response_id: 'reconnect-next-response',
    turn: 3,
    event_sequence_no: 5,
    total_input_tokens: 110120,
    total_cached_tokens: 570,
    total_output_tokens: 218,
    cache_hit_rate: 570 / 110120
  }
  let holdNextListRefresh = false
  let capturedPending = null
  let listSnapshot
  const harness = await createHarness(t, {
    sessions: [createSession('A', { source: 'ws', llm_request_metadata: mainMetadata })],
    sessionsList: ({ pending, sessions }) => {
      if (holdNextListRefresh) {
        holdNextListRefresh = false
        capturedPending = pending
        return pending.promise
      }
      return listSnapshot ?? sessions
    },
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness

  await harness.flush()
  await receiveWs(harness, {
    type: 'llm_request_metadata',
    session_id: 'A',
    ...mainMetadata
  })
  await receiveWs(harness, { type: 'connection_closed' })
  harness.sessions.find(item => item.session_id === 'A').llm_request_metadata = {
    ...persistedMetadata
  }
  await receiveWs(harness, { type: 'connection_reopened' })
  holdNextListRefresh = true
  await receiveWs(harness, { type: 'resume_complete', session_id: 'A' })
  assert.ok(capturedPending)

  await receiveWs(harness, {
    type: 'llm_request_metadata',
    session_id: 'A',
    ...expectedNextMetadata
  })
  capturedPending.resolve([
    createSession('A', { source: 'ws', llm_request_metadata: persistedMetadata })
  ])
  await harness.flush()
  assert.deepEqual(snapshotMetadata(chat.llmRequestMetadata.value), expectedNextMetadata)

  const refreshSnapshot = async snapshot => {
    listSnapshot = snapshot
    await chat.loadSessions()
    await harness.flush()
    assert.deepEqual(snapshotMetadata(chat.llmRequestMetadata.value), expectedNextMetadata)
  }

  for (const snapshot of [
    [createSession('A', { source: 'ws', llm_request_metadata: persistedMetadata })],
    [createSession('A', { source: 'ws', llm_request_metadata: {} })],
    [createSession('A', {
      source: 'ws',
      llm_request_metadata: {
        total_input_tokens: -1,
        total_cached_tokens: 0,
        total_output_tokens: 0,
        cache_hit_rate: 0
      }
    })]
  ]) {
    await refreshSnapshot(snapshot)
  }

  await refreshSnapshot(new Error('session list temporarily unavailable'))
  await refreshSnapshot([
    createSession('A', { source: 'ws', llm_request_metadata: expectedNextMetadata })
  ])
})

test('composed reconnect keeps recovered metadata isolated across session switches', async t => {
  const mainMetadata = createMainMetadata()
  const taskTotals = {
    total_input_tokens: 50120,
    total_cached_tokens: 270,
    total_output_tokens: 207,
    cache_hit_rate: 270 / 50120
  }
  const recoveredMetadata = { ...mainMetadata, ...taskTotals }
  const otherMetadata = {
    ...createMainMetadata({ responseId: 'other-response', workSequenceNo: 1, eventSequenceNo: 1 }),
    input_tokens: 7000,
    context_window_tokens: 16384,
    max_output_tokens: 4096,
    output_tokens: 70,
    cached_tokens: 70,
    total_input_tokens: 7000,
    total_cached_tokens: 70,
    total_output_tokens: 70,
    cache_hit_rate: 70 / 7000
  }
  let holdNextListRefresh = false
  let capturedPending = null
  const harness = await createHarness(t, {
    sessions: [
      createSession('A', { source: 'ws', llm_request_metadata: mainMetadata }),
      createSession('B', { source: 'ws', llm_request_metadata: otherMetadata })
    ],
    sessionsList: ({ pending, sessions }) => {
      if (holdNextListRefresh) {
        holdNextListRefresh = false
        capturedPending = pending
        return pending.promise
      }
      return sessions
    },
    initialRoute: { path: '/', query: { session_id: 'A' } }
  })
  const { chat } = harness
  const sessionsSnapshot = (aMetadata = mainMetadata) => [
    createSession('A', { source: 'ws', llm_request_metadata: aMetadata }),
    createSession('B', { source: 'ws', llm_request_metadata: otherMetadata })
  ]

  await harness.flush()
  await receiveWs(harness, {
    type: 'llm_request_metadata',
    session_id: 'A',
    ...mainMetadata
  })
  await receiveWs(harness, { type: 'connection_closed' })
  const serverSession = harness.sessions.find(item => item.session_id === 'A')
  serverSession.llm_request_metadata = { ...recoveredMetadata }
  serverSession.messages.push({
    id: 1301,
    role: 'assistant',
    response_id: 'isolated-task-response',
    content: 'isolated task result'
  })
  await receiveWs(harness, { type: 'connection_reopened' })
  holdNextListRefresh = true
  await receiveWs(harness, { type: 'resume_complete', session_id: 'A' })
  assert.ok(capturedPending)

  await selectSession(harness, 'B')
  assert.equal(chat.currentSessionId.value, 'B')
  assert.deepEqual(snapshotMetadata(chat.llmRequestMetadata.value), otherMetadata)

  capturedPending.resolve(sessionsSnapshot(recoveredMetadata))
  await harness.flush()
  assert.deepEqual(snapshotMetadata(chat.llmRequestMetadata.value), otherMetadata)
  assert.deepEqual(
    snapshotMetadata(chat.sessions.value.find(item => item.session_id === 'A').llm_request_metadata),
    recoveredMetadata
  )

  await selectSession(harness, 'A')
  await harness.flush()
  assert.equal(chat.currentSessionId.value, 'A')
  assert.deepEqual(snapshotMetadata(chat.llmRequestMetadata.value), recoveredMetadata)
  assert.equal(chat.llmRequestMetadata.value.total_input_tokens, 50120)
  assert.equal(chat.messages.value.filter(message => message.content === 'isolated task result').length, 1)
})
