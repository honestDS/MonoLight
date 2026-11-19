import assert from 'node:assert/strict'
import test from 'node:test'

import { createHarness, createSession } from '../../chatDraftHarness.js'
import {
  SESSION_A,
  SESSION_B,
  WORK_ID,
  FINAL_CONTENT,
  FINAL_REASONING,
  persistedMessage,
  createPersistedHistory,
  createWsHarness,
  receiveEvent,
  sendInput,
  assertSingleReconciledReply,
  selectRealSession
} from './messageReconciliationHarness.js'

test('reconciles a terminal proactive reply that precedes resumed turn_end after session switch', async t => {
  let terminalHistoryAvailable = false
  const harness = await createHarness(t, {
    sessions: [
      createSession(SESSION_A, {
        source: 'ws',
        is_loading: true,
        is_reply_running: true
      }),
      createSession(SESSION_B, { source: 'ws' })
    ],
    initialRoute: { path: '/', query: { session_id: SESSION_B } },
    sessionsHistory: ({ sessionId }) => {
      if (sessionId !== SESSION_A) return []
      return terminalHistoryAvailable
        ? createPersistedHistory()
        : [persistedMessage({ id: 41, role: 'user', content: '你好' })]
    }
  })
  await harness.flush()

  await selectRealSession(harness, SESSION_A)
  assert.ok(harness.wsManager.sent.some(message => (
    message.type === 'resume' && message.session_id === SESSION_A
  )))

  const requestId = 'request-switch-terminal-race'
  const responseId = 'response-switch-terminal-race'
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
    type: 'reasoning',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: FINAL_REASONING,
    event_sequence_no: 2
  })
  await receiveEvent(harness, {
    type: 'content',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: FINAL_CONTENT,
    event_sequence_no: 3
  })

  terminalHistoryAvailable = true
  await receiveEvent(harness, {
    type: 'proactive_reply',
    session_id: SESSION_A,
    request_ids: [requestId],
    work_id: WORK_ID,
    event_sequence_no: 187,
    message_id: 50,
    content: FINAL_CONTENT,
    source: 'foreground'
  })
  await harness.flush()

  await receiveEvent(harness, {
    type: 'turn_end',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: FINAL_CONTENT,
    reasoning_content: FINAL_REASONING,
    message_id: 50,
    finish_reason: 'stop',
    event_sequence_no: 4
  })

  assertSingleReconciledReply(harness.chat.messages.value)
})

test('replays an A stream after a real B switch without letting old event identities drop text', async t => {
  const harness = await createHarness(t, {
    sessions: [
      createSession(SESSION_A, { source: 'ws' }),
      createSession(SESSION_B, { source: 'ws' })
    ],
    initialRoute: { path: '/', query: { session_id: SESSION_A } },
    sessionsHistory: () => []
  })
  await harness.flush()

  const requestId = await sendInput(harness, '恢复流')
  const responseId = 'response-resumed'
  const firstEvents = [
    {
      type: 'agent_loop_start',
      session_id: SESSION_A,
      request_id: requestId,
      request_ids: [requestId],
      work_id: WORK_ID,
      response_id: responseId,
      turn: 1,
      event_sequence_no: 1
    },
    {
      type: 'agent_loop_output',
      session_id: SESSION_A,
      request_id: requestId,
      request_ids: [requestId],
      work_id: WORK_ID,
      response_id: responseId,
      turn: 1,
      event_sequence_no: 2
    },
    {
      type: 'reasoning',
      session_id: SESSION_A,
      request_id: requestId,
      work_id: WORK_ID,
      response_id: responseId,
      turn: 1,
      content: '原思考',
      event_sequence_no: 3
    },
    {
      type: 'content',
      session_id: SESSION_A,
      request_id: requestId,
      work_id: WORK_ID,
      response_id: responseId,
      turn: 1,
      content: '原文',
      event_sequence_no: 4
    }
  ]
  for (const event of firstEvents) await receiveEvent(harness, event)

  await selectRealSession(harness, SESSION_B)
  await selectRealSession(harness, SESSION_A)

  for (const event of firstEvents) await receiveEvent(harness, event, true)
  await receiveEvent(harness, {
    type: 'reasoning',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '继续思考',
    event_sequence_no: 5
  })
  await receiveEvent(harness, {
    type: 'content',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '继续',
    event_sequence_no: 6
  })

  const resumedStreamReply = harness.chat.messages.value.find(message => (
    message.role === 'assistant' && message.response_id === responseId
  ))
  assert.ok(resumedStreamReply)
  assert.equal(resumedStreamReply.content, '原文继续')
  const visibleReasoning = harness.chat.messages.value.filter(message => (
    message.response_id === responseId
    && typeof message.reasoning_content === 'string'
  ))
  assert.equal(visibleReasoning.length, 1)
  assert.equal(
    Array.from(visibleReasoning, message => message.reasoning_content).join(''),
    '原思考继续思考'
  )

  await receiveEvent(harness, {
    type: 'turn_end',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '原文继续',
    reasoning_content: '原思考继续思考',
    message_id: 70,
    finish_reason: 'stop',
    event_sequence_no: 7
  })

  const finalReplies = harness.chat.messages.value.filter(message => message.role === 'assistant')
  assert.equal(finalReplies.length, 1)
  assert.equal(finalReplies[0].content, '原文继续')
  assert.equal(finalReplies[0].reasoning_content, '原思考继续思考')
  assert.equal(String(finalReplies[0].db_id), '70')
  assert.equal(harness.chat.messages.value.some(message => message.role === 'reasoning'), false)
  assert.equal(harness.chat.messages.value.some(message => message.role === 'thinking'), false)
})

test('preserves a stream through reconnect and finalizes one reasoning source', async t => {
  const harness = await createWsHarness(t)
  await harness.flush()

  const requestId = await sendInput(harness, '断线重连')
  const responseId = 'response-reconnect'
  const start = {
    type: 'agent_loop_start',
    session_id: SESSION_A,
    request_id: requestId,
    request_ids: [requestId],
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    event_sequence_no: 1
  }
  const output = {
    type: 'agent_loop_output',
    session_id: SESSION_A,
    request_id: requestId,
    request_ids: [requestId],
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    event_sequence_no: 2
  }
  const firstReasoning = {
    type: 'reasoning',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '前思',
    event_sequence_no: 3
  }
  const firstContent = {
    type: 'content',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '前文',
    event_sequence_no: 4
  }
  await receiveEvent(harness, start)
  await receiveEvent(harness, output)
  await receiveEvent(harness, firstReasoning)
  await receiveEvent(harness, firstContent)

  harness.receiveWs({ type: 'connection_closed', session_id: SESSION_A })
  harness.receiveWs({ type: 'connection_reopened', session_id: SESSION_A })
  await harness.flush()
  assert.ok(harness.wsManager.sent.some(message => message.type === 'resume'))

  await receiveEvent(harness, start, true)
  await receiveEvent(harness, output, true)
  await receiveEvent(harness, firstReasoning, true)
  await receiveEvent(harness, firstContent, true)
  await receiveEvent(harness, {
    type: 'reasoning',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '后思',
    event_sequence_no: 5
  })
  await receiveEvent(harness, {
    type: 'content',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '后文',
    event_sequence_no: 6
  })
  await receiveEvent(harness, {
    type: 'turn_end',
    session_id: SESSION_A,
    request_id: requestId,
    work_id: WORK_ID,
    response_id: responseId,
    turn: 1,
    content: '前文后文',
    reasoning_content: '前思后思',
    message_id: 50,
    finish_reason: 'stop',
    event_sequence_no: 7
  })

  const messages = harness.chat.messages.value
  const assistants = messages.filter(message => message.role === 'assistant')
  const reasoningSources = messages.filter(message => (
    typeof message.reasoning_content === 'string' && message.reasoning_content.trim()
  ))
  assert.equal(assistants.length, 1)
  assert.equal(assistants[0].content, '前文后文')
  assert.equal(assistants[0].reasoning_content, '前思后思')
  assert.equal(reasoningSources.length, 1)
  assert.equal(messages.filter(message => message.role === 'reasoning').length, 0)
  assert.equal(messages.filter(message => message.role === 'thinking').length, 0)
})
