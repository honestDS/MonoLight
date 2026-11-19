import assert from 'node:assert/strict'
import test from 'node:test'

import { createHarness, createSession } from '../../chatDraftHarness.js'
import {
  SESSION_A,
  SESSION_B,
  WORK_ID,
  persistedMessage,
  createWsHarness,
  receiveEvent,
  sendInput,
  auditHistoryUpdate
} from './messageReconciliationHarness.js'

test('keeps same-content replies separate when database and response identities differ', async t => {
  const harness = await createWsHarness(t)
  await harness.flush()

  const sendReply = async ({ requestText, workId, responseId, messageId }) => {
    const requestId = await sendInput(harness, requestText, workId)
    await receiveEvent(harness, {
      type: 'agent_loop_start',
      session_id: SESSION_A,
      request_id: requestId,
      request_ids: [requestId],
      work_id: workId,
      response_id: responseId,
      turn: 1,
      event_sequence_no: 1
    })
    await receiveEvent(harness, {
      type: 'content',
      session_id: SESSION_A,
      request_id: requestId,
      work_id: workId,
      response_id: responseId,
      turn: 1,
      content: '相同正文',
      event_sequence_no: 2
    })
    await receiveEvent(harness, {
      type: 'turn_end',
      session_id: SESSION_A,
      request_id: requestId,
      work_id: workId,
      response_id: responseId,
      turn: 1,
      content: '相同正文',
      message_id: messageId,
      finish_reason: 'stop',
      event_sequence_no: 3
    })
    await receiveEvent(harness, {
      type: 'done',
      session_id: SESSION_A,
      request_ids: [requestId],
      work_id: workId,
      response_id: `session-reply-work:${workId}`,
      event_sequence_no: 4,
      response: {
        choices: [{
          message: { role: 'assistant', content: '相同正文' },
          finish_reason: 'stop'
        }],
        history: [],
        work_id: workId,
        message_id: messageId
      }
    })
  }

  await sendReply({
    requestText: '第一次',
    workId: 7,
    responseId: 'response-same-1',
    messageId: 61
  })
  await sendReply({
    requestText: '第二次',
    workId: 8,
    responseId: 'response-same-2',
    messageId: 62
  })

  const replies = harness.chat.messages.value.filter(message => (
    message.role === 'assistant' && message.content === '相同正文'
  ))
  assert.equal(replies.length, 2)
  assert.deepEqual(
    Array.from(replies, message => message.response_id),
    ['response-same-1', 'response-same-2']
  )
  assert.deepEqual(
    new Set(replies.map(message => String(message.db_id))),
    new Set(['61', '62'])
  )
})

const createBackgroundWatchResult = (id, overrides = {}) => persistedMessage({
  id,
  role: 'tool',
  type: 'background_result',
  content: JSON.stringify({
    type: 'background_tool_result',
    task: `自动观察结果-${id}`,
    tool_call: {
      id: `watch-${id}`,
      name: 'background_task',
      arguments: '{}'
    }
  }),
  ...overrides
})

const createHistoryMergeHarness = async (t, initialHistory, latestHistory) => {
  const state = { latest: false }
  const harness = await createHarness(t, {
    sessions: [
      createSession(SESSION_A, { source: 'ws' }),
      createSession(SESSION_B, { source: 'ws' })
    ],
    initialRoute: { path: '/', query: { session_id: SESSION_A } },
    sessionsHistory: ({ sessionId }) => {
      if (sessionId !== SESSION_A) return []
      return state.latest ? latestHistory : initialHistory
    }
  })
  await harness.flush()
  return { harness, state }
}

const messageIds = messages => Array.from(
  messages,
  message => String(message.db_id ?? message.id)
)

const messageSnapshot = messages => Array.from(messages, message => ({
  id: String(message.db_id ?? message.id),
  role: message.role,
  type: message.type,
  content: message.content,
  created_at: message.created_at
}))

for (const eventType of ['audit_tool_results_update', 'tool_results_update']) {
  test(`inserts a late automatic watch result before persisted history on ${eventType} and stays idempotent`, async t => {
    const initialHistory = [
      persistedMessage({
        id: 170,
        role: 'assistant',
        type: 'tool_call',
        content: JSON.stringify({
          role: 'assistant',
          content: '',
          tool_calls: [{ id: 'call-watch', name: 'background_task', arguments: '{}' }]
        }),
        tool_calls: [{ id: 'call-watch', name: 'background_task', arguments: '{}' }]
      }),
      persistedMessage({
        id: 171,
        role: 'tool',
        type: 'tool_result',
        content: JSON.stringify({
          role: 'tool',
          tool_call_id: 'call-watch',
          content: '工具结果'
        }),
        tool_call_id: 'call-watch'
      }),
      persistedMessage({ id: 172, role: 'assistant', content: '最终回复' })
    ]
    const latestHistory = [createBackgroundWatchResult(169), ...initialHistory]
    const { harness, state } = await createHistoryMergeHarness(t, initialHistory, latestHistory)

    assert.deepEqual(messageIds(Array.from(harness.chat.messages.value)), ['170', '171', '172'])

    state.latest = true
    const update = { ...auditHistoryUpdate(), type: eventType }
    await receiveEvent(harness, update)

    const firstMessages = Array.from(harness.chat.messages.value)
    const firstPersisted = firstMessages.filter(message => (
      ['169', '170', '171', '172'].includes(String(message.db_id))
    ))
    assert.deepEqual(messageIds(firstPersisted), ['169', '170', '171', '172'])
    assert.equal(new Set(messageIds(firstPersisted)).size, 4)
    assert.equal(new Set(Array.from(firstPersisted, message => message.content)).size, 4)
    assert.equal(firstPersisted[0].type, 'background_result')
    assert.equal(JSON.parse(firstPersisted[0].content).task, '自动观察结果-169')

    const firstSnapshot = messageSnapshot(firstMessages)
    await receiveEvent(harness, { ...update, messages: [] })
    assert.deepEqual(messageSnapshot(Array.from(harness.chat.messages.value)), firstSnapshot)
  })
}

test('orders late persisted history by created_at before db_id', async t => {
  const initialHistory = [
    persistedMessage({ id: 170, role: 'user', content: '消息170', created_at: 100 }),
    persistedMessage({ id: 171, role: 'user', content: '消息171', created_at: 300 }),
    persistedMessage({ id: 172, role: 'user', content: '消息172', created_at: 400 })
  ]
  const latestHistory = [
    persistedMessage({ id: 169, role: 'user', content: '消息169', created_at: 200 }),
    ...initialHistory
  ]
  const { harness, state } = await createHistoryMergeHarness(t, initialHistory, latestHistory)

  state.latest = true
  await receiveEvent(harness, auditHistoryUpdate())

  assert.deepEqual(messageIds(Array.from(harness.chat.messages.value)), ['170', '169', '171', '172'])
})

test('keeps same-created_at persisted messages in db_id order', async t => {
  const initialHistory = [
    persistedMessage({ id: 170, role: 'user', content: '消息170', created_at: 500 }),
    persistedMessage({ id: 172, role: 'user', content: '消息172', created_at: 500 })
  ]
  const latestHistory = [
    persistedMessage({ id: 172, role: 'user', content: '消息172', created_at: 500 }),
    persistedMessage({ id: 171, role: 'user', content: '消息171', created_at: 500 }),
    persistedMessage({ id: 170, role: 'user', content: '消息170', created_at: 500 })
  ]
  const { harness, state } = await createHistoryMergeHarness(t, initialHistory, latestHistory)

  state.latest = true
  await receiveEvent(harness, auditHistoryUpdate())

  assert.deepEqual(messageIds(Array.from(harness.chat.messages.value)), ['170', '171', '172'])
})

test('falls back to persisted db_id order when created_at is missing', async t => {
  const initialHistory = [
    persistedMessage({ id: 170, role: 'user', content: '消息170', created_at: undefined }),
    persistedMessage({ id: 172, role: 'user', content: '消息172', created_at: undefined })
  ]
  const latestHistory = [
    persistedMessage({ id: 172, role: 'user', content: '消息172', created_at: undefined }),
    persistedMessage({ id: 171, role: 'user', content: '消息171', created_at: undefined }),
    persistedMessage({ id: 170, role: 'user', content: '消息170', created_at: undefined })
  ]
  const { harness, state } = await createHistoryMergeHarness(t, initialHistory, latestHistory)

  state.latest = true
  await receiveEvent(harness, auditHistoryUpdate())

  assert.deepEqual(messageIds(Array.from(harness.chat.messages.value)), ['170', '171', '172'])
})

test('prepends a history page without reversing it or moving live stream placeholders', async t => {
  const historyState = { older: false }
  const initialHistory = [
    persistedMessage({ id: 170, role: 'user', content: '消息170' }),
    persistedMessage({ id: 171, role: 'user', content: '消息171' }),
    persistedMessage({ id: 172, role: 'user', content: '消息172' })
  ]
  const olderHistory = [
    persistedMessage({ id: 167, role: 'user', content: '消息167' }),
    persistedMessage({ id: 168, role: 'user', content: '消息168' }),
    persistedMessage({ id: 169, role: 'user', content: '消息169' })
  ]
  const harness = await createHarness(t, {
    sessions: [
      createSession(SESSION_A, { source: 'ws' }),
      createSession(SESSION_B, { source: 'ws' })
    ],
    initialRoute: { path: '/', query: { session_id: SESSION_A } },
    sessionsHistory: ({ sessionId }) => {
      if (sessionId !== SESSION_A) return []
      return historyState.older ? olderHistory : initialHistory
    }
  })
  await harness.flush()

  const requestId = await sendInput(harness, '保留流式占位')
  const responseId = 'response-pagination-placeholder'
  await receiveEvent(harness, {
    type: 'agent_loop_start',
    session_id: SESSION_A,
    request_id: requestId,
    request_ids: [requestId],
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    event_sequence_no: 1
  })
  await receiveEvent(harness, {
    type: 'content',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '未持久化流式正文',
    event_sequence_no: 2
  })

  const beforeMessages = Array.from(harness.chat.messages.value)
  const beforeTransient = beforeMessages.filter(message => message.db_id === undefined)
  const beforeTransientIds = Array.from(beforeTransient, message => String(message.id))
  assert.equal(beforeTransient.some(message => message.role === 'thinking'), true)
  assert.equal(beforeTransient.some(message => (
    message.role === 'assistant' && message.response_id === responseId
  )), true)

  harness.chat.messageList.value = {
    scrollTop: 0,
    addEventListener: () => {},
    removeEventListener: () => {},
    captureScrollAnchor: () => ({ top: 0 }),
    restoreScrollAnchor: async () => {}
  }
  harness.chat.hasMore.value = true
  historyState.older = true
  await harness.chat.handleScroll()

  const afterMessages = Array.from(harness.chat.messages.value)
  const afterPersisted = afterMessages.filter(message => message.db_id !== undefined)
  const afterTransient = afterMessages.filter(message => message.db_id === undefined)
  assert.deepEqual(messageIds(afterPersisted), ['167', '168', '169', '170', '171', '172'])
  assert.deepEqual(Array.from(afterTransient, message => String(message.id)), beforeTransientIds)
  assert.deepEqual(
    Array.from(afterMessages.slice(-beforeTransient.length), message => String(message.id)),
    beforeTransientIds
  )
})
